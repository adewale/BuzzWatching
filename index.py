from google.appengine.ext import db
from google.appengine.ext import deferred
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.api import users
from google.appengine.api import urlfetch
from google.appengine.ext.webapp.util import run_wsgi_app
import os
import logging

from google.appengine.api.labs import taskqueue

# This is where simplejson lives on App Engine
from django.utils import simplejson
MAX_TASK_RETRIES = 10

class Event(db.Model):
    data = db.TextProperty()
    consumed = db.DateTimeProperty(auto_now_add=True)

    def inflate_json(self):
        self.json = simplejson.loads(self.data)

    def title(self):
        return self.json['title']

    def source(self):
        return self.json['object']['links']['alternate'][0]['href']

    def profile_image(self):
        return self.json['actor']['thumbnailUrl']

    def profile_url(self):
        return self.json['actor']['profileUrl']

    def content(self):
        return self.json['object']['content']

def handle_result_(rpc):
    pass
def handle_result(rpc):
    result = rpc.get_result()
    logging.info('Result of rpc had status: %s' % result.status_code)
    content = simplejson.loads(result.content)

    if not content['data'].has_key('items'):
        return
    events = []
    for item in content['data']['items']:
        id = item['id']
        updated = item['updated']
        #logging.info('id: %s and updated: %s' % (id, updated))
        event = Event(data = simplejson.dumps(item), key_name=id)
        events.append(event)
    db.put(events)

def create_callback(rpc):
    return lambda: handle_result(rpc)

def find_events(search_term, radius):
    # This: http://json-indent.appspot.com/indent?url=https://www.googleapis.com/buzz/v1/activities/search%3Flat%3D51.49510480761027%26lon%3D-0.1463627815246582%26radius%3D500%26alt%3Djson is easier to read
    # Pull in all hits for hackcamp or activities which are within radius of the office
    urls = []
    radius_search = 'https://www.googleapis.com/buzz/v1/activities/search?lat=51.49510480761027&lon=-0.1463627815246582&radius=%s&alt=json' % radius
    urls.append(radius_search)
    term_search = 'https://www.googleapis.com/buzz/v1/activities/search?q=%s&alt=json' % search_term
    urls.append(term_search)
    
    # Basic idea comes straight from: http://code.google.com/appengine/docs/python/urlfetch/asynchronousrequests.html
    rpcs = []
    for url in urls:
        rpc = urlfetch.create_rpc()
        rpc.callback = create_callback(rpc)
        urlfetch.make_fetch_call(rpc, url)
        rpcs.append(rpc)

    # Process all the async calls and wait for stragglers
    for rpc in rpcs:
        rpc.wait()

def get_events(event):
    # TODO
    # Make this a background task
    # Stop hardcoding the event and the location
    logging.info('event was <%s>' % event)
    tokens = event.split('/')
    if len(tokens) > 1:
        event = tokens[1]
    #find_events('hackcamp', 500)
    #find_events(event, 500)
    taskqueue.add(url='/bgtasks', params={'event':'hackcamp', 'radius':500})

    query = Event.all()
    query.order('-consumed')
    events = query.fetch(10)
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
                radius = self.request.get('radius')
		find_events(event_name, radius)
handlers = [
('/bgtasks', BackGroundTaskHandler),
('/events/(.*)', EventTagHandler),
('/', IndexHandler)]
application = webapp.WSGIApplication(handlers, debug = True)

def main():
	run_wsgi_app(application)

if __name__ == '__main__':
	main()
