from google.appengine.ext import db
from google.appengine.ext import deferred
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.api import urlfetch
from google.appengine.ext.webapp.util import run_wsgi_app
import os
import logging
import iso8601
import time
import urllib

from google.appengine.api.labs import taskqueue

# This is where simplejson lives on App Engine
from django.utils import simplejson
MAX_TASK_RETRIES = 10

class Event(db.Model):
    data = db.TextProperty()
    consumed = db.DateTimeProperty(auto_now_add=True)
    created = db.DateTimeProperty()

    def inflate_json(self):
        self.json = simplejson.loads(self.data)

    def _title(self):
        return self.json['title']

    def source(self):
        if self.json.has_key('object'):
            return self.json['object']['links']['alternate'][0]['href']
        elif self.json.has_key('from_user'):
            return u'http://twitter.com/%s/statuses/%s' % (self.json['from_user'], self.json['id'])
        else:
            return u'http://www.flickr.com/photos/%s/%s' % (self.json['owner']['username'], self.json['id'])

    def profile_image(self):
        if self.json.has_key('actor'):
            return self.json['actor']['thumbnailUrl']
        elif self.json.has_key('from_user'):
            return self.json['profile_image_url']
        else:
            return u'http://farm%s.static.flickr.com/%s/buddyicons/%s.jpg' % (self.json['farm'], self.json['server'], self.json['owner']['nsid'])

    def profile_url(self):
        if self.json.has_key('actor'):
            return self.json['actor']['profileUrl']
        elif self.json.has_key('from_user'):
            return u'http://twitter.com/%s' % self.json['from_user']
        else:
            return u'http://www.flickr.com/photos/%s' % (self.json['owner']['nsid'])

    def content(self):
        if self.json.has_key('object'):
            return self.json['object']['content']
        if self.json.has_key('text'):
            return self.json['text']
        elif self.json.has_key('title'):
            return self.json['title']
        

def parse_date(date_str):
    return iso8601.parse_date(date_str)

def has_data(content):
    content['source'] = ''

    # google buzz
    if content.has_key('data') and content['data'].has_key('items'):
        content['source'] = 'buzz'
        return True

    # twitter
    if content.has_key('results'):
        content['source'] = 'twitter'
        return True

    # flickr
    if content.has_key('query') and content['query'].has_key('results'):
        content['source'] = 'flickr'
        return True

    return False

def parse_buzz(content):
    # google buzz
    events = []
    for item in content['data']['items']:
        id = item['id']
        updated = parse_date(item['updated'])
        event = Event(data = simplejson.dumps(item), key_name=id, created=updated)
        events.append(event)
    return events

def parse_twitter(content):
    # twitter
    events = []
    for item in content['results']:
        id = str(item['id'])
        parsed = time.strptime(
            item['created_at'].replace('+0000', '').strip(),
            "%a, %d %b %Y %H:%M:%S"
        )
        created = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            parsed
        )
        created = parse_date(created)
        
        event = Event(data = simplejson.dumps(item), key_name=id, created=created)
        events.append(event)
    return events

def parse_flickr(content):
    # flickr
    events = []
    for item in content['query']['results']['photo']:
        id = str(item['id'])
        taken = parse_date(item['dates']['taken'])
        event = Event(data = simplejson.dumps(item), key_name=id, created=taken)
        events.append(event)
    return events

def handle_result(rpc, url):
    try:
        result = rpc.get_result()
    except urlfetch.DownloadError, e:
        logging.error('Unable to download %s due to %s' % (url, e))
        return []
    logging.info('Result of rpc had status: %s' % result.status_code)
    content = simplejson.loads(result.content)

    if not has_data(content):
        return []

    fx = 'parse_%s' % content['source']
    if fx in globals():
        events = globals()[fx](content)
        return events


def find_events(search_term):
    urls = []
    term_search = 'https://www.googleapis.com/buzz/v1/activities/search?q=%s&alt=json' % search_term
    urls.append(term_search)

    # Twitter search - watch those rate limits!
    term_search = 'http://search.twitter.com/search.json?q=%s' % search_term
    urls.append(term_search)

    # Flickr search - Using YQL as Flickr's API actually wants an API key for search and returns very little as a response! How rude...
    term_search = 'http://query.yahooapis.com/v1/public/yql?q='+ urllib.quote('select id, owner, server , farm , title , dates , secret ,  urls  from flickr.photos.info where photo_id in (select id from flickr.photos.search where text') + '="%s")&format=json' % search_term
    urls.append(term_search)

    rpcs = []
    for url in urls:
        rpc = urlfetch.create_rpc()
        urlfetch.make_fetch_call(rpc, url)
        rpcs.append((rpc, url))

    # Process all the async calls and wait for stragglers
    all_events = []
    for rpc, url in rpcs:
        all_events.extend(handle_result(rpc, url))
    db.put(all_events)

def get_events(event):
    # TODO
    # Make this a background task
    # Stop hardcoding the event
    logging.info('event was <%s>' % event)
    tokens = event.split('/')
    if len(tokens) > 1:
        event = tokens[1]
    taskqueue.add(url='/bgtasks', params={'event':'hackcamp'})

    query = Event.all()
    query.order('-created')
    events = query.fetch(100)
    for event in events:
        event.inflate_json()
    return events

class IndexHandler(webapp.RequestHandler):
    def get(self):
        template_values = {}
        path = os.path.join(os.path.dirname(__file__), 'static/events.html')
        self.response.out.write(template.render(path, template_values))

class EventTagHandler(webapp.RequestHandler):
    def get(self, event):
        template_values = {}
        if not event:
            template_values['events'] = []
        else:
            events = get_events(event)
            template_values['events'] = events
            logging.info('Found %d events' % len(events))
        path = os.path.join(os.path.dirname(__file__), 'static/events.html')
        self.response.out.write(template.render(path, template_values))

class BackGroundTaskHandler(webapp.RequestHandler):
	def post(self):
		logging.info("Request body %s" % self.request.body)
		retryCount = self.request.headers.get('X-AppEngine-TaskRetryCount')
		taskName = self.request.headers.get('X-AppEngine-TaskName')
		if retryCount and int(retryCount) > MAX_TASK_RETRIES:
			logging.warning("Abandoning this task: %s after %s retries" % (taskName, retryCount))
			return
                event_name  = self.request.get('event')
		find_events(event_name)
handlers = [
('/bgtasks', BackGroundTaskHandler),
('/events/(.*)', EventTagHandler),
('/', IndexHandler)]
application = webapp.WSGIApplication(handlers, debug = True)

def main():
	run_wsgi_app(application)

if __name__ == '__main__':
	main()
