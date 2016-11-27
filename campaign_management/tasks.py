from django.core.mail import get_connection, EmailMultiAlternatives
from celery import shared_task, group

from subscriber_management.models import List, Subscriber
from campaign_management.models import Campaign

import time
from bs4 import BeautifulSoup as HTMLParser
import base64


def _inject_unsubscribe_link(subscriber, campaign):
    unsubscribe_link = subscriber.unsubscribe_link()
    html_email = campaign.html_email_for_sending.replace('*%7CUNSUB%7C*',
                                             unsubscribe_link)
    text_email = campaign.text_email.replace('*%7CUNSUB%7C*',
                                             unsubscribe_link)
    html_email = campaign.html_email_for_sending.replace('*|UNSUB|*',
                                             unsubscribe_link)
    text_email = campaign.text_email.replace('*|UNSUB|*',
                                             unsubscribe_link)
    return html_email, text_email, unsubscribe_link


def _inject_tracking_pixel(subscriber, email_list, campaign):
    pixel_url = "http://minimail.fullweb.io/pixou/open/?list={}&campaign={}&subscriber={}"\
                .format(email_list.uuid, campaign.uuid, subscriber.uuid)
    tracking_snippet = '<img src="{}" height=1 width=1 /></body'.format(pixel_url)
    html_email = campaign.html_email_for_sending.replace('</body', tracking_snippet)
    return html_email


def _convert_links_for_tracking(subscriber, email_list, campaign):
    try:
        html_email = campaign.html_email_for_sending
        link_tmpl = "http://minimail.fullweb.io/pixou/click/?list={}&campaign={}&subscriber={}&uri={}"
        html_tree = HTMLParser(html_email, "html5lib")
        all_links = html_tree.findAll('a')
        for link in all_links:
            if not link or not link.get('href') or not link.get('href', '').startswith('http'):
                continue
            try:
                ori_href=  link.get('href').encode('utf8')
                b64_link = base64.b64encode(ori_href).decode('utf8')
                link['href'] = link_tmpl.format(email_list.uuid, campaign.uuid,
                                                subscriber.uuid, b64_link)
            except Exception as ex:
                continue
        html_email = html_tree.prettify(formatter=None)
    except Exception as ex:
        print(ex)
    finally:
        return html_email


def _gen_campaign_emails(campaign_id, list_id):
    """_gen_campaign_emails

    :param campaign_id:
    :param list_id:
    """
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
        email_list = List.objects.get(id=list_id)
        subscribers = Subscriber.objects.filter(list__id=list_id, validated=True)
        # Build email headers common to all subscribers
        email_headers = {}
        email_headers['X-Mailer'] = "Minimail Mailer"
        email_headers['List-Id'] = "MINIMAIL-{}".format(email_list.uuid)
        email_headers['X-Campaign-Id'] = "MINIMAIL-{}".format(campaign.uuid)
        # Format sender field
        _from = "{} <{}>".format(campaign.email_from_name,
                                 campaign.email_list.from_email)
    except Exception as ex:
        print(ex)
        return tuple() # empty iterable

    for subscriber in subscribers:
        try:
            # TODO: Remember at what byte offset we inject the unsubscribe_link,
            #       to prevent O(n) search each time
            # ----
            # Add custom tracking open pixel for this subscriber
            html_email = _inject_tracking_pixel(subscriber, email_list, campaign)
            # Convert all links to track clicks
            html_email = _convert_links_for_tracking(subscriber, email_list, campaign)
            # Add custom unsubscribe link for this subscriber
            html_email, text_email, unsubscribe_link = _inject_unsubscribe_link(subscriber,
                                                                                campaign)
            # Build current email headers
            curr_email_headers = {}
            curr_email_headers['List-Unsubscribe'] = "<{}>".format(unsubscribe_link)
            curr_email_headers['X-Recipient-ID'] = "{}".format(subscriber.uuid)
            curr_email_headers.update(email_headers)
            # Create email message object
            email = EmailMultiAlternatives(campaign.email_subject,  # email subject
                                           text_email,  # body text version
                                           _from,  # from sender
                                           [subscriber.email],  # recipient
                                           reply_to=[campaign.email_reply_to_email],
                                           headers=curr_email_headers)
            email.attach_alternative(html_email, "text/html")  # adding html email
        except Exception as ex:
            print(ex)
            email = False
        finally:
            yield email


@shared_task
def _send_one_newsletter_to_one_list(campaign_id, list_id):
    # Start the SMTP connection
    smtp_connection = get_connection()
    smtp_connection.open()
    # Send all emails
    for email in _gen_campaign_emails(campaign_id, list_id):
        try:
            email.send()
        except Exception as ex:
            # TODO: Log fail to generate email
            print(email.to, ex)
            continue
    # Close connection
    smtp_connection.close()
