# This file is part of Cockpit.
#
# Copyright (C) 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio
import contextlib
import errno
import os
import signal
import subprocess
import sys
import unittest.mock
from typing import Any, List, Optional, Tuple

import pytest

import cockpit.transports


class Protocol(cockpit.transports.SubprocessProtocol):
    transport: Optional[asyncio.Transport] = None
    paused: bool = False
    sent: int = 0
    received: int = 0
    exited: bool = False
    close_on_eof: bool = True
    eof: bool = False
    exc: Optional[Exception] = None
    output: Optional[List[bytes]] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport

    def connection_lost(self, exc: Optional[Exception] = None) -> None:
        self.transport = None
        self.exc = exc

    def data_received(self, data: bytes) -> None:
        if self.output is not None:
            self.output.append(data)
        self.received += len(data)

    def eof_received(self) -> bool:
        self.eof = True
        return not self.close_on_eof

    def pause_writing(self) -> None:
        self.paused = True

    def write_until_backlogged(self) -> None:
        while not self.paused:
            self.write(b'a' * 4096)

    def write(self, data: bytes) -> None:
        assert self.transport is not None
        self.transport.write(data)
        self.sent += len(data)

    def write_a_lot(self) -> None:
        assert self.transport is not None
        self.write_until_backlogged()
        assert self.transport.get_write_buffer_size() != 0
        for _ in range(20):
            self.write(b'b' * 1024 * 1024)
        assert self.transport.get_write_buffer_size() > 20 * 1024 * 1024

    def process_exited(self) -> None:
        self.exited = True

    def get_output(self) -> bytes:
        assert self.output is not None
        return b''.join(self.output)

    async def eof_and_exited_with_code(self, returncode) -> None:
        self.close_on_eof = False  # otherwise we won't get process_exited()
        transport = self.transport
        assert isinstance(transport, cockpit.transports.SubprocessTransport)
        while not self.exited or not self.eof:
            await asyncio.sleep(0.1)
        assert transport.get_returncode() == returncode


class TestSpooler:
    @pytest.mark.asyncio
    async def test_bad_fd(self) -> None:
        # Make sure failing to construct succeeds without further failures
        loop = asyncio.get_running_loop()
        with pytest.raises(OSError) as raises:
            cockpit.transports.Spooler(loop, -1)
        assert raises.value.errno == errno.EBADF

    def create_spooler(self, to_write: bytes = b'') -> cockpit.transports.Spooler:
        loop = asyncio.get_running_loop()
        reader, writer = os.pipe()
        try:
            spooler = cockpit.transports.Spooler(loop, reader)
        finally:
            os.close(reader)
        try:
            os.write(writer, to_write)
        finally:
            os.close(writer)
        return spooler

    @pytest.mark.asyncio
    async def test_poll_eof(self) -> None:
        spooler = self.create_spooler()
        while spooler._fd != -1:
            await asyncio.sleep(0.1)
        assert spooler.get() == b''

    @pytest.mark.asyncio
    async def test_nopoll_eof(self) -> None:
        spooler = self.create_spooler()
        assert spooler.get() == b''
        assert spooler._fd == -1

    @pytest.mark.asyncio
    async def test_poll_small(self) -> None:
        spooler = self.create_spooler(b'abcd')
        while spooler._fd != -1:
            await asyncio.sleep(0.1)
        assert spooler.get() == b'abcd'

    @pytest.mark.asyncio
    async def test_nopoll_small(self) -> None:
        spooler = self.create_spooler(b'abcd')
        assert spooler.get() == b'abcd'
        assert spooler._fd == -1

    @pytest.mark.asyncio
    async def test_big(self) -> None:
        loop = asyncio.get_running_loop()
        reader, writer = os.pipe()
        try:
            spooler = cockpit.transports.Spooler(loop, reader)
        finally:
            os.close(reader)

        try:
            os.set_blocking(writer, False)
            written = 0
            blob = b'a' * 64 * 1024  # NB: pipe buffer is 64k
            while written < 1024 * 1024:
                # Note: we should never get BlockingIOError here since we always
                # give the reader a chance to drain the pipe.
                written += os.write(writer, blob)
                while len(spooler.get()) < written:
                    await asyncio.sleep(0.01)

            assert spooler._fd != -1
        finally:
            os.close(writer)

        await asyncio.sleep(0.1)
        assert spooler._fd == -1

        assert len(spooler.get()) == written


class TestEpollLimitations:
    # https://github.com/python/cpython/issues/73903
    #
    # There are some types of files that epoll doesn't work with, returning
    # EPERM.  We might be in a situation where we receive one of those on
    # stdin/stdout for AsyncioTransport, so we'd theoretically like to support
    # them.
    async def spool_file(self, filename: str) -> None:
        loop = asyncio.get_running_loop()
        with open(filename) as fp:
            spooler = cockpit.transports.Spooler(loop, fp.fileno())
        while spooler._fd != -1:
            await asyncio.sleep(0.1)

    @pytest.mark.xfail
    @pytest.mark.asyncio
    async def test_read_file(self) -> None:
        await self.spool_file(__file__)

    @pytest.mark.xfail
    @pytest.mark.asyncio
    async def test_dev_null(self) -> None:
        await self.spool_file('/dev/null')


class TestStdio:
    @contextlib.contextmanager
    def create_terminal(self):
        ours, theirs = os.openpty()
        stdin = os.dup(theirs)
        stdout = os.dup(theirs)
        os.close(theirs)
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        yield ours, protocol, cockpit.transports.StdioTransport(loop, protocol, stdin=stdin, stdout=stdout)
        os.close(stdin)
        os.close(stdout)

    @pytest.mark.asyncio
    async def test_terminal_write_eof(self):
        # Make sure write_eof() fails
        with self.create_terminal() as (ours, _protocol, transport):
            assert not transport.can_write_eof()
            with pytest.raises(RuntimeError):
                transport.write_eof()
            os.close(ours)

    @pytest.mark.asyncio
    async def test_terminal_disconnect(self):
        # Make sure disconnecting the session shows up as an EOF
        with self.create_terminal() as (ours, protocol, _transport):
            os.close(ours)
            while not protocol.eof:
                await asyncio.sleep(0.1)


class TestSubprocessTransport:
    def subprocess(self, args, **kwargs: Any) -> Tuple[Protocol, cockpit.transports.SubprocessTransport]:
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        transport = cockpit.transports.SubprocessTransport(loop, protocol, args, **kwargs)
        assert transport._protocol == protocol
        assert protocol.transport == transport
        return protocol, transport

    @pytest.mark.asyncio
    async def test_true(self) -> None:
        protocol, transport = self.subprocess(['true'])
        await protocol.eof_and_exited_with_code(0)
        assert transport.get_stderr() == ''

    @pytest.mark.asyncio
    async def test_cat(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.close_on_eof = False
        protocol.write_a_lot()
        assert transport.can_write_eof()
        transport.write_eof()
        await protocol.eof_and_exited_with_code(0)
        assert protocol.transport is not None  # should not have automatically closed
        assert transport.get_returncode() == 0
        assert protocol.sent == protocol.received
        transport.close()
        # make sure the connection_lost handler isn't called immediately
        assert protocol.transport is not None
        # ...but "soon" (in the very next mainloop iteration)
        await asyncio.sleep(0.01)
        assert protocol.transport is None

    @pytest.mark.asyncio
    async def test_send_signal(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        transport.send_signal(signal.SIGINT)
        await protocol.eof_and_exited_with_code(-signal.SIGINT)

    @pytest.mark.asyncio
    async def test_pid(self) -> None:
        protocol, transport = self.subprocess(['sh', '-c', 'echo $$'])
        protocol.output = []
        await protocol.eof_and_exited_with_code(0)
        assert int(protocol.get_output()) == transport.get_pid()

    @pytest.mark.asyncio
    async def test_terminate(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        transport.kill()
        await protocol.eof_and_exited_with_code(-signal.SIGKILL)

        protocol, transport = self.subprocess(['cat'])
        transport.terminate()
        await protocol.eof_and_exited_with_code(-signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stderr(self) -> None:
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        transport = cockpit.transports.SubprocessTransport(loop, protocol, ['cat', '/nonexistent'],
                                                           stderr=subprocess.PIPE)
        await protocol.eof_and_exited_with_code(1)
        assert protocol.received == protocol.sent == 0
        # Unless we reset it, we should get the same result repeatedly
        assert '/nonexistent' in transport.get_stderr()
        assert '/nonexistent' in transport.get_stderr()
        assert '/nonexistent' in transport.get_stderr(reset=True)
        # After we reset, it should be the empty string
        assert transport.get_stderr() == ''
        assert transport.get_stderr(reset=True) == ''

    @pytest.mark.asyncio
    async def test_safe_watcher_ENOSYS(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # this test disables pidfd support in order to force the fallback path
        # which creates a SafeChildWatcher.  That's deprecated since 3.12 and
        # removed in 3.14, so skip this test on those versions to avoid issues.
        if sys.version_info >= (3, 12, 0):
            pytest.skip()

        monkeypatch.setattr(os, 'pidfd_open', unittest.mock.Mock(side_effect=OSError), raising=False)
        protocol, _transport = self.subprocess(['true'])
        await protocol.eof_and_exited_with_code(0)

    @pytest.mark.asyncio
    async def test_true_pty(self) -> None:
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        transport = cockpit.transports.SubprocessTransport(loop, protocol, ['true'], pty=True)
        assert not transport.can_write_eof()
        await protocol.eof_and_exited_with_code(0)
        assert protocol.received == protocol.sent == 0

    @pytest.mark.asyncio
    async def test_broken_pipe(self) -> None:
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        transport = cockpit.transports.SubprocessTransport(loop, protocol, ['true'])
        protocol.close_on_eof = False
        while not protocol.exited:
            await asyncio.sleep(0.1)

        assert protocol.transport is transport  # should not close on EOF

        # Now let's write to the stdin with the other side closed.
        # This should be enough to immediately disconnect us (EPIPE)
        protocol.write(b'abc')
        # make sure the connection_lost handler isn't called immediately
        assert protocol.transport is not None
        # ...but "soon" (in the very next mainloop iteration)
        await asyncio.sleep(0.01)
        assert protocol.transport is None
        assert isinstance(protocol.exc, BrokenPipeError)

    @pytest.mark.asyncio
    async def test_broken_pipe_backlog(self) -> None:
        loop = asyncio.get_running_loop()
        protocol = Protocol()
        transport = cockpit.transports.SubprocessTransport(loop, protocol, ['cat'])
        protocol.close_on_eof = False

        # Since we're not reading, cat's stdout will back up and it will be
        # forced to stop reading at some point.  We'll still have a rather full
        # write buffer.
        protocol.write_a_lot()

        # This will result in the stdin closing.  Our next attempt to write to
        # the buffer should end badly (EPIPE).
        transport.kill()

        while protocol.transport:
            await asyncio.sleep(0.1)

        assert protocol.transport is None
        assert isinstance(protocol.exc, BrokenPipeError)

    @pytest.mark.asyncio
    async def test_window_size(self) -> None:
        protocol, transport = self.subprocess(['bash', '-ic',
                                               """
                                                   while true; do
                                                       sleep 0.1
                                                       echo ${LINES}x${COLUMNS}
                                                   done
                                               """],
                                              pty=True,
                                              window=cockpit.transports.WindowSize({'rows': 22, 'cols': 33}))
        protocol.output = []
        while b'22x33\r\n' not in protocol.get_output():
            await asyncio.sleep(0.1)

        transport.set_window_size(cockpit.transports.WindowSize({'rows': 44, 'cols': 55}))
        while b'44x55\r\n' not in protocol.get_output():
            await asyncio.sleep(0.1)

        transport.close()

    @pytest.mark.asyncio
    async def test_env(self) -> None:
        protocol, transport = self.subprocess(['bash', '-ic', 'echo $HOME'],
                                              pty=True,
                                              env={'HOME': '/test'})
        protocol.output = []
        while b'/test\r\n' not in protocol.get_output():
            await asyncio.sleep(0.1)

        transport.close()

    @pytest.mark.asyncio
    async def test_simple_close(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.output = []

        protocol.write(b'abcd')
        assert protocol.transport
        assert protocol.transport.get_write_buffer_size() == 0
        protocol.transport.close()
        # make sure the connection_lost handler isn't called immediately
        assert protocol.transport is not None
        # ...but "soon" (in the very next mainloop iteration)
        await asyncio.sleep(0.01)
        assert protocol.transport is None
        # we have another ref on the transport
        transport.close()  # should be idempotent

    @pytest.mark.asyncio
    async def test_flow_control(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.output = []

        protocol.write(b'abcd')
        assert protocol.transport is not None
        transport.pause_reading()
        await asyncio.sleep(0.1)
        transport.resume_reading()
        while protocol.received < 4:
            await asyncio.sleep(0.1)
        assert protocol.transport is not None
        transport.write_eof()
        protocol.close_on_eof = False
        while not protocol.eof:
            await asyncio.sleep(0.1)
        assert not transport.is_reading()
        transport.pause_reading()  # no-op
        assert not transport.is_reading()
        transport.resume_reading()  # no-op
        assert not transport.is_reading()

    @pytest.mark.asyncio
    async def test_write_backlog_eof(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.output = []
        protocol.write_a_lot()

        assert transport.can_write_eof()
        transport.write_eof()
        assert not transport.is_closing()
        while protocol.transport is not None:
            await asyncio.sleep(0.1)
        assert protocol.transport is None

    @pytest.mark.asyncio
    async def test_write_backlog_close(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.output = []
        protocol.write_a_lot()

        assert transport
        transport.close()
        assert transport.is_closing()
        # FIXME: closing the channel should kill the process, like asyncio's SubprocessTransport
        # See https://github.com/cockpit-project/cockpit/pull/18340
        transport.kill()
        while protocol.transport is not None:
            await asyncio.sleep(0.1)
        assert protocol.transport is None

    @pytest.mark.asyncio
    async def test_write_backlog_eof_and_close(self) -> None:
        protocol, transport = self.subprocess(['cat'])
        protocol.output = []
        protocol.write_a_lot()

        assert transport
        transport.write_eof()
        transport.close()
        assert protocol.transport
        assert protocol.transport.is_closing()
        # FIXME: closing the channel should kill the process, like asyncio's SubprocessTransport
        # See https://github.com/cockpit-project/cockpit/pull/18340
        transport.kill()
        while protocol.transport is not None:
            await asyncio.sleep(0.1)
        assert protocol.transport is None
