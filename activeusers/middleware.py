from datetime import datetime, timedelta
import logging
import random
import re
import time
import traceback
import urllib, urllib2

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db.utils import DatabaseError
from django.http import Http404
from activeusers import utils
from activeusers.models import Visitor

title_re = re.compile('<title>(.*?)</title>')
log = logging.getLogger('activeusers.middleware')

class VisitorTrackingMiddleware:
    """
    Keeps track of your active users.  Anytime a visitor accesses a valid URL,
    their unique record will be updated with the page they're on and the last
    time they requested a page.

    Records are considered to be unique when the session key and IP address
    are unique together.  Sometimes the same user used to have two different
    records, so I added a check to see if the session key had changed for the
    same IP and user agent in the last 5 minutes
    """

    def process_request(self, request):
        # don't process AJAX requests
        if request.is_ajax(): return

        # create some useful variables
        ip_address = utils.get_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]

        if hasattr(request, 'session'):
            # use the current session key if we can
            session_key = request.session.session_key
        else:
            # otherwise just fake a session key
            session_key = '%s:%s' % (ip_address, user_agent)

        prefixes = getattr(settings, 'ACTIVEUSERS_IGNORE_PREFIXES', [])

        # ensure that the request.path does not begin with any of the prefixes
        for prefix in prefixes:
            if request.path.startswith(prefix):
                log.debug('Not tracking request to: %s' % request.path)
                return

        # if we get here, the URL needs to be tracked
        # determine what time it is
        now = datetime.now()

        attrs = {
            'session_key': session_key,
            'ip_address': ip_address
        }

        # for some reason, Visitor.objects.get_or_create was not working here
        try:
            visitor = Visitor.objects.get(**attrs)
        except Visitor.DoesNotExist:
            # see if there's a visitor with the same IP and user agent
            # within the last 5 minutes
            cutoff = now - timedelta(minutes=5)
            visitors = Visitor.objects.filter(
                ip_address=ip_address,
                user_agent=user_agent,
                last_update__gte=cutoff
            )

            if len(visitors):
                visitor = visitors[0]
                visitor.session_key = session_key
                log.debug('Using existing visitor for IP %s / UA %s: %s' % (ip_address, user_agent, visitor.id))
            else:
                # it's probably safe to assume that the visitor is brand new
                visitor = Visitor(**attrs)
                log.debug('Created a new visitor: %s' % attrs)
        except:
            return

        # determine whether or not the user is logged in
        user = request.user
        if isinstance(user, AnonymousUser):
            user = None

        # update the tracking information
        visitor.user = user
        visitor.user_agent = user_agent

        # if the visitor record is new, or the visitor hasn't been here for
        # at least an hour, update their referrer URL
        one_hour_ago = now - timedelta(hours=1)
        if not visitor.last_update or visitor.last_update <= one_hour_ago:
            visitor.referrer = utils.u_clean(request.META.get('HTTP_REFERER', 'unknown')[:255])

            # reset the number of pages they've been to
            visitor.page_views = 0
            visitor.session_start = now

        visitor.url = request.path
        visitor.page_views += 1
        visitor.last_update = now
        try:
            visitor.save()
        except DatabaseError:
            log.error('There was a problem saving visitor information:\n%s\n\n%s' % (traceback.format_exc(), locals()))

class VisitorCleanUpMiddleware:
    """Clean up old visitor tracking records in the database"""

    def process_request(self, request):

        last_clean_time = cache.get('activeusers_last_cleanup')
        now = datetime.now()
        x_minutes_ago = now - timedelta(minutes=int(utils.get_timeout()) / 2)

        if not last_clean_time or last_clean_time <= x_minutes_ago:
            cache.set('activeusers_last_cleanup', now)

            timeout = utils.get_cleanup_timeout()
            if str(timeout).isdigit():
                timeout = datetime.now() - timedelta(hours=int(timeout))
                Visitor.objects.filter(last_update__lte=timeout).delete()
