##############################################################################
#
# Copyright (c) 2002 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

from __future__ import absolute_import

import logging
import os
import re
from socket import gethostbyaddr
import sys
import socket

import ZConfig
from ZConfig.components.logger import loghandler
from zope.event import notify
from zope.processlifetime import ProcessStarting
import ZServer.Zope2.Startup.config

try:
    IO_ERRORS = (IOError, OSError, WindowsError)
except NameError:
    IO_ERRORS = (IOError, OSError, )

logger = logging.getLogger("Zope")


class ZopeStarter(object):
    """This is a class which starts Zope with a ZServer."""

    wsgi = False

    def __init__(self):
        self.event_logger = logging.getLogger()
        # We log events to the root logger, which is backed by a
        # "StartupHandler" log handler.  The "StartupHandler" buffers
        # log messages.  When the "real" loggers are set up, we flush
        # accumulated messages in StartupHandler's buffers to the real
        # logger.
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            "%Y-%m-%d %H:%M:%S")
        self.debug_handler = loghandler.StreamHandler()
        self.debug_handler.setFormatter(formatter)
        self.debug_handler.setLevel(logging.WARN)

        self.startup_handler = loghandler.StartupHandler()

        self.event_logger.addHandler(self.debug_handler)
        self.event_logger.addHandler(self.startup_handler)

    def prepare(self):
        self.setupInitialLogging()
        self.setupLocale()
        self.setupSecurityOptions()
        self.setupPublisher()
        # Start ZServer servers before we drop privileges so we can bind
        # to "low" ports:
        self.setupZServer()
        self.setupServers()
        # drop privileges after setting up servers
        self.dropPrivileges()
        self.setupFinalLogging()
        self.makeLockFile()
        self.makePidFile()
        self.setupInterpreter()
        self.startZope()
        self.serverListen()
        from App.config import getConfiguration
        config = getConfiguration()  # NOQA
        self.registerSignals()
        logger.info('Ready to handle requests')
        self.sendEvents()

    def dropPrivileges(self):
        return dropPrivileges(self.cfg)

    def run(self):
        # the mainloop.
        try:
            from App.config import getConfiguration
            config = getConfiguration()  # NOQA
            import Lifetime
            Lifetime.loop()
            from ZServer.Zope2.Startup.config import ZSERVER_EXIT_CODE
            sys.exit(ZSERVER_EXIT_CODE)
        finally:
            self.shutdown()

    def shutdown(self):
        databases = getattr(self.cfg.dbtab, 'databases', {})
        for db in databases.values():
            db.close()
        self.unlinkLockFile()
        self.unlinkPidFile()

    def getLoggingLevel(self):
        if self.cfg.eventlog is None:
            level = logging.INFO
        else:
            # get the lowest handler level.  This is the effective level
            # level at which which we will spew messages to the console
            # during startup.
            level = self.cfg.eventlog.getLowestHandlerLevel()
        return level

    def registerSignals(self):
        from Signals import Signals
        Signals.registerZopeSignals([self.cfg.eventlog,
                                     self.cfg.access,
                                     self.cfg.trace])

    def sendEvents(self):
        notify(ProcessStarting())

    def setConfiguration(self, cfg):
        self.cfg = cfg

    def setupConfiguredLoggers(self):
        # Must happen after ZopeStarter.setupInitialLogging()
        self.event_logger.removeHandler(self.startup_handler)
        if self.cfg.eventlog is not None:
            self.cfg.eventlog()
        if self.cfg.access is not None:
            self.cfg.access()
        if self.cfg.trace is not None:
            self.cfg.trace()

        # flush buffered startup messages to event logger
        if self.cfg.debug_mode:
            self.event_logger.removeHandler(self.debug_handler)
            self.startup_handler.flushBufferTo(self.event_logger)
            self.event_logger.addHandler(self.debug_handler)
        else:
            self.startup_handler.flushBufferTo(self.event_logger)

    def setupFinalLogging(self):
        pass

    def setupInitialLogging(self):
        if self.cfg.debug_mode:
            self.debug_handler.setLevel(self.getLoggingLevel())
        else:
            self.event_logger.removeHandler(self.debug_handler)
            self.debug_handler = None

    def setupInterpreter(self):
        # make changes to the python interpreter environment
        sys.setcheckinterval(self.cfg.python_check_interval)

    def setupLocale(self):
        # set a locale if one has been specified in the config
        if not self.cfg.locale:
            return

        # workaround to allow unicode encoding conversions in DTML
        import codecs
        dummy = codecs.lookup('utf-8')  # NOQA

        locale_id = self.cfg.locale

        if locale_id is not None:
            try:
                import locale
            except:
                raise ZConfig.ConfigurationError(
                    'The locale module could not be imported.\n'
                    'To use localization options, you must ensure\n'
                    'that the locale module is compiled into your\n'
                    'Python installation.')
            try:
                locale.setlocale(locale.LC_ALL, locale_id)
            except:
                raise ZConfig.ConfigurationError(
                    'The specified locale "%s" is not supported by your'
                    'system.\nSee your operating system documentation for '
                    'more\ninformation on locale support.' % locale_id)

    def setupPublisher(self):
        import ZPublisher.HTTPRequest
        import ZPublisher.Publish
        ZPublisher.Publish.set_default_debug_mode(self.cfg.debug_mode)
        ZPublisher.Publish.set_default_authentication_realm(
            self.cfg.http_realm)
        if self.cfg.trusted_proxies:
            mapped = []
            for name in self.cfg.trusted_proxies:
                mapped.extend(_name_to_ips(name))
            ZPublisher.HTTPRequest.trusted_proxies = tuple(mapped)
            ZServer.Zope2.Startup.config.TRUSTED_PROXIES = tuple(mapped)
        from ZServer.ZPublisher.exceptionhook import EXCEPTION_HOOK
        import Zope2
        setattr(Zope2, 'zpublisher_exception_hook', EXCEPTION_HOOK)

    def setupSecurityOptions(self):
        import AccessControl
        AccessControl.setImplementation(
            self.cfg.security_policy_implementation)
        AccessControl.setDefaultBehaviors(
            not self.cfg.skip_ownership_checking,
            not self.cfg.skip_authentication_checking,
            self.cfg.verbose_security)

    def setupZServer(self):
        # Increase the number of threads
        ZServer.Zope2.Startup.config.setNumberOfThreads(
            self.cfg.zserver_threads)
        ZServer.Zope2.Startup.config.ZSERVER_CONNECTION_LIMIT = \
            self.cfg.max_listen_sockets

    def serverListen(self):
        for server in self.cfg.servers:
            if hasattr(server, 'fast_listen'):
                # This one has the delayed listening feature
                if not server.fast_listen:
                    server.fast_listen = True
                    # same value as defined in medusa.http_server.py
                    server.listen(1024)

    def setupServers(self):
        socket_err = (
            'There was a problem starting a server of type "%s". '
            'This may mean that your user does not have permission to '
            'bind to the port which the server is trying to use or the '
            'port may already be in use by another application. '
            '(%s)')
        servers = []
        for server in self.cfg.servers:
            # create the server from the server factory
            # set up in the config
            try:
                servers.append(server.create())
            except socket.error as e:
                raise ZConfig.ConfigurationError(
                    socket_err % (server.servertype(), e[1]))
        self.cfg.servers = servers

    def makeLockFile(self):
        if not self.cfg.zserver_read_only_mode:
            # lock_file is used for the benefit of zctl-like systems, so they
            # can tell whether Zope is already running before attempting to
            # fire it off again.
            #
            # We aren't concerned about locking the file to protect against
            # other Zope instances running from our CLIENT_HOME, we just
            # try to lock the file to signal that zctl should not try to
            # start Zope if *it* can't lock the file; we don't panic
            # if we can't lock it.
            # we need a separate lock file because on win32, locks are not
            # advisory, otherwise we would just use the pid file
            from ZServer.Zope2.Startup.utils import lock_file
            lock_filename = self.cfg.lock_filename
            try:
                if os.path.exists(lock_filename):
                    os.unlink(lock_filename)
                self.lockfile = open(lock_filename, 'w')
                lock_file(self.lockfile)
                self.lockfile.write(str(os.getpid()))
                self.lockfile.flush()
            except IO_ERRORS:
                pass

    def makePidFile(self):
        if not self.cfg.zserver_read_only_mode:
            # write the pid into the pidfile if possible
            try:
                if os.path.exists(self.cfg.pid_filename):
                    os.unlink(self.cfg.pid_filename)
                f = open(self.cfg.pid_filename, 'w')
                f.write(str(os.getpid()))
                f.close()
            except IO_ERRORS:
                pass

    def unlinkPidFile(self):
        if not self.cfg.zserver_read_only_mode:
            try:
                os.unlink(self.cfg.pid_filename)
            except OSError:
                pass

    def unlinkLockFile(self):
        if not self.cfg.zserver_read_only_mode and hasattr(self, 'lockfile'):
            try:
                self.lockfile.close()
                os.unlink(self.cfg.lock_filename)
            except OSError:
                pass

    def startZope(self):
        # Import Zope
        import ZServer.Zope2
        ZServer.Zope2.startup()

    # XXX does anyone actually use these three?

    def info(self, msg):
        logger.info(msg)

    def panic(self, msg):
        logger.critical(msg)

    def error(self, msg):
        logger.error(msg)


class WindowsZopeStarter(ZopeStarter):

    def setupInitialLogging(self):
        super(WindowsZopeStarter, self).setupInitialLogging()
        self.setupConfiguredLoggers()


class UnixZopeStarter(ZopeStarter):

    def setupInitialLogging(self):
        super(UnixZopeStarter, self).setupInitialLogging()
        level = self.getLoggingLevel()
        self.startup_handler.setLevel(level)

        # set the initial logging level (this will be changed by the
        # ZConfig settings later)
        self.event_logger.setLevel(level)

    def setupFinalLogging(self):
        super(UnixZopeStarter, self).setupFinalLogging()
        self.setupConfiguredLoggers()


def dropPrivileges(cfg):
    # Drop root privileges if we have them and we're on a posix platform.
    # This needs to be a function so it may be used outside of Zope
    # appserver startup (e.g. from zopectl debug)
    if os.name != 'posix':
        return

    if os.getuid() != 0:
        return

    import pwd

    effective_user = cfg.effective_user
    if effective_user is None:
        msg = ('A user was not specified to setuid to; fix this to '
               'start as root (change the effective-user directive '
               'in zope.conf)')
        logger.critical(msg)
        raise ZConfig.ConfigurationError(msg)

    try:
        uid = int(effective_user)
    except ValueError:
        try:
            pwrec = pwd.getpwnam(effective_user)
        except KeyError:
            msg = "Can't find username %r" % effective_user
            logger.error(msg)
            raise ZConfig.ConfigurationError(msg)
        uid = pwrec[2]
    else:
        try:
            pwrec = pwd.getpwuid(uid)
        except KeyError:
            msg = "Can't find uid %r" % uid
            logger.error(msg)
            raise ZConfig.ConfigurationError(msg)
    gid = pwrec[3]

    if uid == 0:
        msg = 'Cannot start Zope with the effective user as the root user'
        logger.error(msg)
        raise ZConfig.ConfigurationError(msg)

    try:
        from os import initgroups
        initgroups(effective_user, gid)
        os.setgid(gid)
    except OSError:
        logger.exception('Could not set group id of effective user')

    os.setuid(uid)
    logger.info('Set effective user to "%s"' % effective_user)
    return 1  # for unit testing purposes


def _name_to_ips(host, _is_ip=re.compile(r'(\d+\.){3}').match):
    '''map a name *host* to the sequence of its ip addresses;
    use *host* itself (as sequence) if it already is an ip address.
    Thus, if only a specific interface on a host is trusted,
    identify it by its ip (and not the host name).
    '''
    if _is_ip(host):
        return [host]
    return gethostbyaddr(host)[2]
