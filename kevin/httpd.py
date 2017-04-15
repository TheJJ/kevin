"""
Web server to receive WebHook notifications from GitHub,
and provide them in a job queue.
"""

from abc import abstractmethod

from tornado import websocket, web, gen
from tornado.platform.asyncio import AsyncIOMainLoop
from tornado.queues import Queue

from .build import get_build
from .config import CFG
from .service import Trigger
from .update import (
    JobCreated, JobUpdate, JobState,
    StdOut, BuildState, BuildSource
)
from .watcher import Watcher


class HookTrigger(Trigger):
    """
    Base class for a webhook trigger (e.g. the github thingy).
    """

    def __init__(self, cfg, project):
        super().__init__(cfg, project)

    @abstractmethod
    def get_handler(self):
        """
        Return the (url, HookHandler class) to register at tornado for webhooks
        """
        pass

    def merge_cfg(self, urlhandlers):
        # when e.g. GitHubHookHandler is instanciated,
        # the list of all Triggers that use it
        # will be passed as configuration.

        # create an entry in the defaultdict for this
        # hook handler class, e.g. GitHubHookHandler.
        handlerkwargs = urlhandlers[self.get_handler()]

        # and add the config which requested it to the list
        # this step creates the mandatory "triggers" constructor
        # argument for all HookHandlers.
        handlerkwargs["triggers"].append(self)

        # additional custom keyword arguments for this
        urlhandlers[self.get_handler()] = handlerkwargs


class HookHandler(web.RequestHandler):
    """
    Base class for web hook handlers.
    A web hook is a http request made by e.g. github, gitlab, ...
    and notify kevin that there's a job to do.
    """

    def initialize(self, triggers):
        """
        triggers: a list of HookTriggers which requested to instanciate
                  this HookHandler
        """
        raise NotImplementedError()

    def get(self):
        raise NotImplementedError()

    def post(self):
        raise NotImplementedError()


def run_httpd(handlers, queue):
    """
    This class contains a server that listens for WebHook
    notifications to spawn triggered actions, e.g. new Builds.
    It also provides the websocket API and plain log streams for curl.

    handlers: (url, handlercls) -> [cfg, cfg, ...]
    queue: the jobqueue.Queue where new builds/jobs are put in
    """
    # use the main asyncio loop to run tornado
    AsyncIOMainLoop().install()

    urlhandlers = dict()
    urlhandlers[("/", PlainStreamHandler)] = None
    urlhandlers[("/ws", WebSocketHandler)] = None

    urlhandlers.update(handlers)

    # create the tornado application
    # that serves assigned urls to handlers.
    handlers = list()
    for (url, handler), cfgs in urlhandlers.items():
        if cfgs is not None:
            handlers.append((url, handler, cfgs))
        else:
            handlers.append((url, handler))

    app = web.Application(handlers)

    app.queue = queue

    # bind to tcp port
    app.listen(CFG.dyn_port, address=str(CFG.dyn_address))


class WebSocketHandler(websocket.WebSocketHandler, Watcher):
    """ Provides a job description stream via WebSocket """
    def open(self):
        self.build = None

        project = CFG.projects[self.get_parameter("project")]
        build_id = self.get_parameter("hash")
        self.build = get_build(project, build_id)

        def get_filter(filter_def):
            """
            Returns a filter function from the filter definition string.
            """
            if not filter_def:
                return lambda _: True
            else:
                job_names = filter_def.split(",")
                return lambda job_name: job_name in job_names

        # state_filter specifies which JobState updates to forward.
        self.state_filter = get_filter(self.get_parameter("state_filter"))
        # filter_ specifies which JobUpdate updates to forward.
        # (except for JobState updates, which are treated by the filter above).
        self.filter_ = get_filter(self.get_parameter("filter"))

        self.build.watch(self)

    def get_parameter(self, name, default=None):
        """
        Returns the string value of the URL parameter with the given name.
        """
        try:
            parameter, = self.request.query_arguments[name]
        except (KeyError, ValueError):
            return default
        else:
            return parameter.decode()

    def on_close(self):
        if self.build is not None:
            self.build.unwatch(self)

    def on_message(self, message):
        # TODO: handle user messages
        pass

    def on_update(self, update):
        """
        Called by the watched build when an update arrives.
        """
        if update is StopIteration:
            self.close()
            return

        if isinstance(update, JobUpdate):
            if isinstance(update, JobCreated):
                # those are not interesting for the webinterface.
                return
            if isinstance(update, JobState):
                filter_ = self.state_filter
            else:
                filter_ = self.filter_

            if filter_(update.job_name):
                self.write_message(update.json())

        elif isinstance(update, (BuildState, BuildSource)):
            # these build-specific updates are never filtered.
            self.write_message(update.json())

    def check_origin(self, origin):
        # Allow connections from anywhere.
        return True


class PlainStreamHandler(web.RequestHandler, Watcher):
    """ Provides the job stdout stream via plain HTTP GET """

    def initialize(self):
        self.job = None

    @gen.coroutine
    def get(self):

        try:
            project_name = self.request.query_arguments["project"][0]
        except (KeyError, IndexError):
            self.write(b"no project given\n")
            return

        try:
            build_id = self.request.query_arguments["hash"][0]
        except (KeyError, IndexError):
            self.write(b"no build hash given\n")
            return

        try:
            job_name = self.request.query_arguments["job"][0]
        except (KeyError, IndexError):
            self.write(b"no job given\n")
            return

        project_name = project_name.decode(errors='replace')
        build_id = build_id.decode(errors='replace')
        job_name = job_name.decode(errors='replace')

        try:
            project = CFG.projects[project_name]

        except KeyError:
            self.write(b"unknown project requested\n")
            return

        build = get_build(project, build_id)
        if not build:
            self.write(("no such build: project %s [%s]\n" % (
                project_name, build_id)).encode())
            return

        self.job = build.jobs.get(job_name)
        if not self.job:
            self.write(("unknown job in project %s [%s]: %s\n" % (
                project_name, build_id, job_name)).encode())
            return

        # the message queue to be sent to the http client
        self.queue = Queue()

        # request the updates from the watched jobs
        self.job.watch(self)

        # emit the updates and wait until no more are coming
        yield self.watch_job()

    @gen.coroutine
    def watch_job(self):
        """ Process updates and send them to the client """

        self.set_header("Content-Type", "text/plain")

        while True:
            update = yield self.queue.get()

            if update is StopIteration:
                break

            if isinstance(update, StdOut):
                self.write(update.data.encode())

            elif isinstance(update, JobState):
                if update.is_errored():
                    self.write(
                        ("\x1b[31merror:\x1b[m %s\n" %
                         (update.text)).encode()
                    )
                elif update.is_succeeded():
                    self.write(
                        ("\x1b[32msuccess:\x1b[m %s\n" %
                         (update.text)).encode()
                    )
                elif update.is_finished():
                    # if finished but not errored or succeeded,
                    # this must be a failure.
                    # TODO: is this a good way to implement this?
                    #       certainly caused me a WTF moment...
                    self.write(
                        ("\x1b[31mfailed:\x1b[m %s\n" %
                         (update.text)).encode()
                    )

            yield self.flush()

        return self.finish()

    def on_update(self, update):
        """ Put a message to the stream queue """
        self.queue.put(update)

    def on_connection_close(self):
        """ Add a connection-end marker to the queue """
        self.on_update(StopIteration)

    def on_finish(self):
        # TODO: only do this if we got a GET request.
        if self.job is not None:
            self.job.unwatch(self)
