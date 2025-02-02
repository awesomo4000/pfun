import asyncio
import datetime
from contextlib import ExitStack
from datetime import timedelta
from subprocess import CalledProcessError
from unittest import mock

import aiohttp
import asynctest
import pytest
from hypothesis import assume, given, settings
from typing_extensions import Protocol

from pfun import (DefaultModules, Dict, Immutable, List, clock, compose,
                  console, effect, either, files, http, identity, logging,
                  random, schedule, sql, state, subprocess)
from pfun.effect import Resource
from pfun.hypothesis_strategies import anything, effects, rights, unaries

from .mocks import MockModules
from .monad_test import MonadTest
from .utils import recursion_limit


class TestEffect(MonadTest):
    @settings(deadline=None)
    @given(
        effects(anything()),
        unaries(effects(anything())),
        unaries(effects(anything())),
        anything()
    )
    def test_associativity_law(self, e, f, g, env):
        assert (
            e.and_then(f).and_then(g).run(env) ==
            e.and_then(lambda x: f(x).and_then(g)).run(env)
        )

    @given(unaries(anything()), unaries(anything()), anything(), anything())
    def test_composition_law(self, f, g, value, env):
        h = compose(f, g)
        assert (
            effect.success(value).map(h).run(env) ==
            effect.success(value).map(g).map(f).run(env)
        )

    @given(anything(), anything())
    def test_identity_law(self, value, env):
        assert (
            effect.success(value).map(identity).run(env) ==
            effect.success(value).run(env)
        )

    @settings(deadline=None)
    @given(unaries(effects(anything())), anything(), anything())
    def test_left_identity_law(self, f, value, env):
        assert (
            effect.success(value).and_then(f).run(env) == f(value).run(env)
        )

    @given(anything(), anything())
    def test_right_identity_law(self, value, env):
        assert (
            effect.success(value).and_then(
                effect.success
            ).run(env) == effect.success(value).run(env)
        )

    @given(anything(), anything())
    def test_equality(self, value, env):
        assert effect.success(value).run(env) == effect.success(value).run(env)

    @settings(deadline=None)
    @given(anything(), anything(), anything())
    def test_inequality(self, first, second, env):
        assume(first != second)
        assert effect.success(first).run(env) != effect.success(second
                                                                ).run(env)

    def test_depend(self):
        assert effect.depend(str).run('env') == 'env'

    def test_from_awaitable(self):
        async def f():
            return 1

        assert effect.from_awaitable(f()).run(None) == 1

    def test_gather(self):
        assert effect.gather_async(
            [effect.success(v) for v in range(3)]
        ).run(None) == (0, 1, 2)

        assert effect.gather(
            [effect.success(v) for v in range(3)]
        ).run(None) == (0, 1, 2)

    def test_gather_generator(self):
        e = effect.gather_async(effect.success(v) for v in range(3))
        assert e.run(None) == (0, 1, 2)
        assert e.run(None) == (0, 1, 2)

    def test_stack_safety(self):
        with recursion_limit(100):
            effect.gather_async([effect.success(v)
                                 for v in range(500)]).run(None)

        e = effect.error('')
        for _ in range(500):
            e = e.recover(lambda _: effect.error(''))
        e = e.recover(lambda _: effect.success(''))
        with recursion_limit(100):
            e.run(None)

        e = effect.success('')
        for _ in range(500):
            e = e.either()
        with recursion_limit(100):
            e.run(None)

    def test_filter(self):
        assert effect.filter_(lambda v: effect.success(v % 2 == 0),
                              range(5)).run(None) == (0, 2, 4)

    def test_filter_async(self):
        assert effect.filter_async(lambda v: effect.success(v % 2 == 0),
                              range(5)).run(None) == (0, 2, 4)

    def test_filter_generator(self):
        e = effect.filter_(
            lambda v: effect.success(v % 2 == 0), (v for v in range(5))
        )
        assert e.run(None) == (0, 2, 4)
        assert e.run(None) == (0, 2, 4)

    def test_for_each(self):
        assert effect.for_each(effect.success, range(3)).run(None) == (0, 1, 2)

    def test_for_each_async(self):
        assert (effect.for_each_async(effect.success, range(3)).run(None)
                == (0, 1, 2))

    def test_either(self):
        success = effect.success(1)
        error = effect.error('error')
        assert success.either().run(None) == either.Right(1)
        assert error.either().run(None) == either.Left('error')

    def test_recover(self):
        success = effect.success(1)
        error = effect.error('error')
        assert success.recover(lambda e: effect.success(2)).run(None) == 1
        assert error.recover(lambda e: effect.success(2)).run(None) == 2

    def test_absolve(self):
        right = either.Right(1)
        left = either.Left('error')
        right_effect = effect.success(right)
        left_effect = effect.success(left)
        assert effect.absolve(right_effect).run(None) == 1
        with pytest.raises(Exception):
            # todo
            effect.absolve(left_effect).run(None)

    def test_error(self):
        with pytest.raises(Exception):
            # todo
            effect.error('error').run(None)

    def test_combine(self):
        def f(a, b):
            return a + b

        assert effect.combine(effect.success('a'),
                              effect.success('b'))(f).run(None) == 'ab'

    def test_combine_async(self):
        def f(a, b):
            return a + b

        assert effect.combine_async(effect.success('a'),
                                    effect.success('b'))(f).run(None) == 'ab'

    def test_lift(self):
        def f(a, b):
            return a + b

        assert effect.lift(f)(effect.success(2),
                              effect.success(2)).run(None) == 4

    def test_lift_async(self):
        def f(a, b):
            return a + b

        assert effect.lift_async(f)(effect.success(2),
                                    effect.success(2)).run(None) == 4

    def test_catch(self):
        def f(fail):
            if fail:
                raise ValueError()
            else:
                return 1

        assert effect.catch(ValueError)(f)(False).run(None) == 1
        catched_error = effect.catch(ValueError)(f)(True)
        with pytest.raises(ValueError):
            catched_error.run(None)

    def test_from_callable(self):
        def f(s: str) -> either.Either[str, str]:
            return either.Right(s)

        async def g(s: str) -> either.Either[str, str]:
            return f(s)

        assert effect.from_callable(f).run('env') == 'env'
        assert effect.from_callable(g).run('env') == 'env'

    def test_memoize(self):
        s = state.State(())
        e = s.modify(
            lambda t: t + ('modify was called', )
        ).discard_and_then(effect.success('result')).memoize()
        double_e = e.discard_and_then(e)
        assert double_e.run(None) == 'result'
        assert s.value == ('modify was called', )

    @settings(deadline=None)
    @given(effects(anything()), effects(anything()))
    def test_lift_cpu_bound(self, e1, e2):
        assert effect.lift_cpu_bound(
            lambda v1, v2: (v1, v2)
        )(e1, e2).run(None) == (e1.run(None), e2.run(None))

    @settings(deadline=None)
    @given(effects(anything()), effects(anything()))
    def test_lift_io_bound(self, e1, e2):
        assert effect.lift_io_bound(
            lambda v1, v2: (v1, v2)
        )(e1, e2).run(None) == (e1.run(None), e2.run(None))

    @settings(deadline=None)
    @given(effects(anything()), effects(anything()))
    def test_combine_cpu_bound(self, e1, e2):
        assert effect.combine_cpu_bound(
            e1, e2
        )(lambda v1, v2: (v1, v2)).run(None) == (e1.run(None), e2.run(None))

    @settings(deadline=None)
    @given(effects(anything()), effects(anything()))
    def test_combine_io_bound(self, e1, e2):
        assert effect.combine_io_bound(
            e1, e2
        )(lambda v1, v2: (v1, v2)).run(None) == (e1.run(None), e2.run(None))

    @settings(deadline=None)
    @given(unaries(rights(anything())))
    def test_from_callable_cpu_bound(self, f):
        assert effect.from_cpu_bound_callable(f).run(None) == f(None).get

    @settings(deadline=None)
    @given(unaries(rights(anything())))
    def test_from_callable_io_bound(self, f):
        assert effect.from_io_bound_callable(f).run(None) == f(None).get

    @settings(deadline=None)
    @given(unaries(anything()))
    def test_catch_cpu_bound(self, f):
        assert effect.catch_cpu_bound(Exception)(f)(None).run(None) == f(None)

    @settings(deadline=None)
    @given(unaries(anything()))
    def test_catch_io_bound(self, f):
        assert effect.catch_io_bound(Exception)(f)(None).run(None) == f(None)

    @pytest.mark.asyncio
    @given(anything())
    async def test_process_and_thread_pool_initialized_lazily(self, value):
        with ExitStack() as stack:
            env = effect.RuntimeEnv(None, stack, 1, 1)
            await effect.success(value).do(env)
            assert env.process_executor is None
            assert env.thread_executor is None

    def test_race(self):
        async def f():
            await asyncio.sleep(10)
            return 'f'

        async def g():
            return 'g'

        e1 = effect.from_awaitable(f())
        e2 = effect.from_awaitable(g())

        assert e1.race(e2).run(None) == 'g'

    def test_timeout(self):
        async def f():
            await asyncio.sleep(10)
            return 'f'

        e = effect.from_awaitable(f())
        with pytest.raises(asyncio.TimeoutError):
            e.timeout(timedelta(milliseconds=1)).run(MockModules())

    def test_repeat(self):
        s = schedule.recurs(2, schedule.spaced(timedelta(seconds=1)))
        assert effect.success(0).repeat(s).run(MockModules()) == (0, 0)

    def test_repeat_error(self):
        s = schedule.recurs(2, schedule.spaced(timedelta(seconds=1)))
        assert (effect.error('whoops').repeat(s).either().run(MockModules()) ==
                either.Left('whoops'))

    def test_retry(self):
        s = schedule.recurs(2, schedule.spaced(timedelta(seconds=1)))
        assert (effect.error('whoops').retry(s).either().run(MockModules()) ==
                either.Left(('whoops', 'whoops')))

    def test_retry_success(self):
        s = schedule.recurs(2, schedule.spaced(timedelta(seconds=1)))
        assert effect.success(0).retry(s).run(MockModules()) == 0

    @settings(deadline=None)
    @given(anything(), unaries(anything()))
    def test_purify(self, x, f):
        assert effect.purify(f)(x).run(None) == f(x)
        assert effect.purify_io_bound(f)(x).run(None) == f(x)
        assert effect.purify_cpu_bound(f)(x).run(None) == f(x)

    def test_provide(self):
        assert effect.depend(str).provide('test').run(None) == 'test'

    def test_provide_provides_first_matching_instance(self):
        assert effect.depend(str).provide('first').run('second') == 'first'

    def test_provide_with_arg(self):
        assert effect.depend(str).provide('test').run(None) == 'test'

    def test_provide_with_protocol(self):
        class P(Protocol):
            x: int

            def f(self):
                pass

        class C:
            x: int
            y: str

            def __init__(self, x, y):
                self.x = x
                self.y = y

            def f(self):
                pass

            def __eq__(self, other):
                return self.x == other.x and self.y == other.y

        assert effect.depend(P).provide(C(0, '')).run(None) == C(0, '')

    def test_provide_with_generic(self):
        effect.depend(List[str]).provide(List([])).run(None) == List([])

    def test_success_repr(self):
        assert repr(effect.success('value')) == 'success(\'value\')'

    def test_error_repr(self):
        assert repr(effect.error('reason')) == 'error(\'reason\')'

    def test_and_then_repr(self):
        f = lambda _: _
        assert (
            repr(effect.success('value').and_then(f)
                 ) == f'success(\'value\').and_then({repr(f)})'
        )

    def test_map_repr(self):
        f = lambda _: _
        assert (
            repr(effect.success('value').map(f)
                 ) == f'success(\'value\').map({repr(f)})'
        )

    def test_discard_and_then_repr(self):
        e = effect.success('value')
        assert (
            repr(e.discard_and_then(e)
                 ) == 'success(\'value\').discard_and_then(success(\'value\'))'
        )

    def test_either_repr(self):
        e = effect.success(0)
        assert repr(e.either()) == 'success(0).either()'

    def test_recover_repr(self):
        e = effect.success(0)
        f = lambda _: _
        assert repr(e.recover(f)) == f'success(0).recover({repr(f)})'

    def test_memoize_repr(self):
        e = effect.success(0)
        assert repr(e.memoize()) == 'success(0).memoize()'

    def test_ensure_repr(self):
        e = effect.success(0)
        assert repr(e.ensure(e)) == 'success(0).ensure(success(0))'

    def test_depend_repr(self):
        assert repr(effect.depend(str)) == f'depend({repr(str)})'

    @pytest.mark.filterwarnings("ignore:coroutine .+ was never awaited")
    def test_from_awaitable_repr(self):
        async def f():
            pass

        a = f()
        assert repr(effect.from_awaitable(a)) == f'from_awaitable({repr(a)})'

    def test_from_callable_repr(self):
        f = lambda _: _
        assert repr(effect.from_callable(f)) == f'from_callable({repr(f)})'

    def test_from_io_bound_callable_repr(self):
        f = lambda _: _
        assert (
            repr(effect.from_io_bound_callable(f)
                 ) == f'from_io_bound_callable({repr(f)})'
        )

    def test_from_cpu_bound_callable_repr(self):
        f = lambda _: _
        assert (
            repr(effect.from_cpu_bound_callable(f)
                 ) == f'from_cpu_bound_callable({repr(f)})'
        )

    def test_catch_repr(self):
        f = lambda _: _
        assert (
            repr(effect.catch(Exception)(f)(0)
                 ) == f'catch(<class \'Exception\'>)({repr(f)})(0)'
        )

    def test_catch_io_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.catch_io_bound(Exception)(f)(0)
                 ) == f'catch_io_bound(<class \'Exception\'>)({repr(f)})(0)'
        )

    def test_catch_cpu_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.catch_cpu_bound(Exception)(f)(0)
                 ) == f'catch_cpu_bound(<class \'Exception\'>)({repr(f)})(0)'
        )

    def test_gather_async_repr(self):
        e = effect.gather_async([effect.success(0)])
        assert (repr(e)) == 'gather_async((success(0),))'

    def test_gather_repr(self):
        e = effect.gather([effect.success(0)])
        assert (repr(e)) == 'gather((success(0),))'

    def test_lift_repr(self):
        f = lambda _: _
        assert (
            repr(effect.lift(f)(effect.success(0))
                 ) == f'lift({repr(f)})(success(0))'
        )

    def test_lift_io_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.lift_io_bound(f)(effect.success(0))
                 ) == f'lift_io_bound({repr(f)})(success(0))'
        )

    def test_lift_cpu_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.lift_cpu_bound(f)(effect.success(0))
                 ) == f'lift_cpu_bound({repr(f)})(success(0))'
        )

    def test_combine_repr(self):
        f = lambda _: _
        assert (
            repr(effect.combine(effect.success(0))(f)
                 ) == f'combine(success(0))({repr(f)})'
        )

    def test_combine_io_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.combine_io_bound(effect.success(0))(f)
                 ) == f'combine_io_bound(success(0))({repr(f)})'
        )

    def test_combine_cpu_bound_repr(self):
        f = lambda _: _
        assert (
            repr(effect.combine_cpu_bound(effect.success(0))(f)
                 ) == f'combine_cpu_bound(success(0))({repr(f)})'
        )

    def test_filter_repr(self):
        f = lambda _: effect.success(True)
        assert repr(effect.filter_(f, [0])) == f'filter_({repr(f)})((0,))'

    def test_for_each_repr(self):
        f = lambda _: effect.success(True)
        assert repr(effect.for_each(f, [0])) == f'for_each({repr(f)})((0,))'

    def test_absolve_repr(self):
        assert repr(effect.absolve(effect.success(0))) == 'absolve(success(0))'

    def test_race_repr(self):
        assert (repr(effect.success(0).race(effect.success(0))) ==
                'success(0).race(success(0))')

    def test_timeout_repr(self):
        assert (repr(effect.success(0).timeout(timedelta(seconds=1))) ==
                'success(0).timeout(datetime.timedelta(seconds=1))')

    def test_map_generator(self):
        def f(_):
            yield from [1, 2, 3]

        assert list(effect.success(None).map(f).run(None)) == [1, 2, 3]


class TestResource:
    def test_get(self):
        mock_resource = asynctest.MagicMock()
        resource = Resource(lambda: either.Right(mock_resource))
        effect = resource.get()
        assert effect.run(None) == mock_resource
        mock_resource.__aenter__.assert_called_once()
        mock_resource.__aexit__.assert_called_once()
        assert resource.resource is None

    def test_resources_are_unique(self):
        mock_resource = asynctest.MagicMock()
        resource = Resource(lambda: either.Right(mock_resource))
        r1, r2 = effect.gather_async(
            (resource.get(), resource.get())
        ).run(None)
        assert r1 is r2
        mock_resource.__aenter__.assert_called_once()


class HasConsole:
    console = console.Console()


def mock_open(read_data=None):
    return mock.patch('pfun.files.open', mock.mock_open(read_data=read_data))


class TestConsole:
    def test_print_line(self, capsys) -> None:

        e = console.print_line('Hello, world!')
        e.run(HasConsole())
        captured = capsys.readouterr()
        assert captured.out == 'Hello, world!\n'

    def test_get_line(self) -> None:
        with mock.patch(
            'pfun.console.input', return_value='Hello!'
        ) as mocked_input:
            e = console.get_line('Say hello')
            assert e.run(HasConsole()) == 'Hello!'
            mocked_input.assert_called_once_with('Say hello')


class HasFiles:
    files = files.Files()


class TestFiles:
    def test_read(self):
        with mock_open('content') as mocked_open:
            e = files.read('foo.txt')
            assert e.run(HasFiles()) == 'content'
        mocked_open.assert_called_once_with('foo.txt')

    def test_write(self):
        with mock_open() as mocked_open:
            e = files.write('foo.txt', 'content')
            e.run(HasFiles())
            mocked_open.assert_called_once_with('foo.txt', 'w')
            mocked_open().write.assert_called_once_with('content')

    def test_read_bytes(self):
        with mock_open(b'content') as mocked_open:
            e = files.read_bytes('foo.txt')
            assert e.run(HasFiles()) == b'content'
            mocked_open.assert_called_once_with('foo.txt', 'rb')

    def test_write_bytes(self):
        with mock_open() as mocked_open:
            e = files.write_bytes('foo.txt', b'content')
            e.run(HasFiles())
            mocked_open.assert_called_once_with('foo.txt', 'wb')
            mocked_open().write.assert_called_once_with(b'content')

    def test_append(self):
        with mock_open() as mocked_open:
            e = files.append('foo.txt', 'content')
            e.run(HasFiles())
            mocked_open.assert_called_once_with('foo.txt', 'a+')
            mocked_open().write.assert_called_once_with('content')

    def test_append_bytes(self):
        with mock_open() as mocked_open:
            e = files.append_bytes('foo.txt', b'content')
            e.run(HasFiles())
            mocked_open.assert_called_once_with('foo.txt', 'ab+')
            mocked_open().write.assert_called_once_with(b'content')


class TestRef:
    def test_get(self):
        int_ref = state.State(0)
        assert int_ref.get().run(None) == 0

    def test_put(self):
        int_ref = state.State(0)
        int_ref.put(1).run(None)
        assert int_ref.value == 1

    def test_modify(self):
        int_ref = state.State(0)
        int_ref.modify(lambda _: 1).run(None)
        assert int_ref.value == 1

    def test_try_modify(self):
        int_ref = state.State(0)
        int_ref.try_modify(lambda _: either.Left('')).either().run(None)
        assert int_ref.value == 0
        int_ref.try_modify(lambda _: either.Right(1)).run(None)
        assert int_ref.value == 1


class HasSubprocess:
    subprocess = subprocess.Subprocess()


class TestSubprocess:
    def test_run_in_shell(self):
        stdout, stderr = subprocess.run_in_shell(
            'echo "test"'
        ).run(
            HasSubprocess()
        )
        assert stdout == b'test\n'

        with pytest.raises(CalledProcessError):
            subprocess.run_in_shell('exit 1').run(HasSubprocess())


class HasLogging:
    logging = logging.Logging()


class TestLogging:
    @mock.patch('pfun.logging.logging')
    def test_get_logger(self, mock_logging):
        logging.get_logger('foo').run(HasLogging())
        mock_logging.getLogger.assert_called_once_with('foo')

    @mock.patch('pfun.logging.logging')
    @pytest.mark.parametrize(
        'log_method',
        ['debug', 'info', 'warning', 'error', 'critical', 'exception']
    )
    def test_logger_methods(self, mock_logging, log_method):
        logging.get_logger('foo').and_then(
            lambda logger: getattr(logger, log_method)('test')
        ).run(HasLogging())
        exc_and_stack_info = log_method == 'exception'
        getattr(
            mock_logging.getLogger('foo'),
            log_method
        ).assert_called_once_with(
            'test', exc_info=exc_and_stack_info, stack_info=exc_and_stack_info
        )  # yapf: disable

    @mock.patch('pfun.logging.logging')
    @pytest.mark.parametrize(
        'log_method',
        ['debug', 'info', 'warning', 'error', 'critical', 'exception']
    )
    def test_logging_methods(self, mock_logging, log_method):
        getattr(logging, log_method)('test').run(HasLogging())
        exc_and_stack_info = log_method == 'exception'
        getattr(mock_logging, log_method).assert_called_once_with(
            'test', exc_info=exc_and_stack_info, stack_info=exc_and_stack_info
        )


class HasHTTP:
    http = http.HTTP()


async def get_awaitable(value):
    return value


class TestHTTP:
    default_params = {
        'params': None,
        'data': None,
        'json': None,
        'cookies': None,
        'headers': None,
        'skip_auto_headers': None,
        'auth': None,
        'allow_redirects': True,
        'max_redirects': 10,
        'compress': None,
        'chunked': None,
        'expect100': False,
        'raise_for_status': None,
        'read_until_eof': True,
        'proxy': None,
        'proxy_auth': None,
        'timeout': aiohttp.client.sentinel,
        'ssl': None,
        'verify_ssl': None,
        'fingerprint': None,
        'ssl_context': None,
        'proxy_headers': None
    }

    def test_get_session(self):
        with asynctest.patch('pfun.http.aiohttp.ClientSession') as session:
            assert http.get_session().run(HasHTTP()) == session()

    @pytest.mark.parametrize(
        'method', ['get', 'put', 'post', 'delete', 'patch', 'head', 'options']
    )
    def test_http_methods(self, method):
        with asynctest.patch('pfun.http.aiohttp.ClientSession') as session:
            read_mock = asynctest.CoroutineMock()
            read_mock.return_value = b'test'
            (
                session.return_value.request.return_value.__aenter__.
                return_value.read
            ) = read_mock
            assert getattr(http, method)('foo.com').run(
                HasHTTP()
            ).content == b'test'
            session().request.assert_called_once_with(
                method, 'foo.com', **self.default_params
            )


class HasSQL:
    sql = sql.SQL('postgres://test@host/test_db')


class TestSQL:
    def test_get_connetion(self):
        with asynctest.patch('pfun.sql.asyncpg.connect') as connect_mock:
            connect_mock.return_value.close = asynctest.CoroutineMock()
            assert sql.get_connection().run(
                HasSQL()
            ) == connect_mock.return_value
            connect_mock.assert_called_once_with(
                'postgres://test@host/test_db'
            )

    def test_execute(self):
        with asynctest.patch('pfun.sql.asyncpg.connect') as connect_mock:
            connect_mock.return_value.close = asynctest.CoroutineMock()
            connect_mock.return_value.execute = asynctest.CoroutineMock(
                return_value='SELECT 1'
            )
            assert sql.execute('select * from users').run(
                HasSQL()
            ) == 'SELECT 1'

    def test_execute_many(self):
        with asynctest.patch('pfun.sql.asyncpg.connect') as connect_mock:
            connect_mock.return_value.close = asynctest.CoroutineMock()
            connect_mock.return_value.executemany = asynctest.CoroutineMock(
                return_value=('SELECT 1', )
            )
            assert sql.execute_many('select * from users',
                                    ['arg']).run(HasSQL()) == ('SELECT 1', )

    def test_fetch_one(self):
        with asynctest.patch('pfun.sql.asyncpg.connect') as connect_mock:
            connect_mock.return_value.close = asynctest.CoroutineMock()
            connect_mock.return_value.fetchrow = asynctest.CoroutineMock(
                return_value={
                    'name': 'bob', 'age': 32
                }
            )
            assert sql.fetch_one('select * from users').run(HasSQL()) == Dict(
                {
                    'name': 'bob', 'age': 32
                }
            )

    def test_fetch(self):
        with asynctest.patch('pfun.sql.asyncpg.connect') as connect_mock:
            connect_mock.return_value.close = asynctest.CoroutineMock()
            connect_mock.return_value.fetch = asynctest.CoroutineMock(
                return_value=({
                    'name': 'bob', 'age': 32
                }, )
            )
            assert sql.fetch('select * from users').run(HasSQL()) == List(
                (Dict({
                    'name': 'bob', 'age': 32
                }), )
            )

    def test_as_type(self):
        class User(Immutable):
            name: str
            age: int

        results = List((Dict({'name': 'bob', 'age': 32}), ))
        assert sql.as_type(User)(results).run(None) == List(
            (User('bob', 32), )
        )


class TestClock:
    def test_sleep(self):
        with asynctest.patch('pfun.effect.asyncio.sleep') as sleep_mock:
            clock.sleep(0).run(DefaultModules())
            sleep_mock.assert_called_with(0)

    def test_now(self):
        with mock.patch('pfun.clock.datetime.datetime') as datetime_mock:
            datetime_mock.now.return_value = (datetime
                                              .datetime.utcfromtimestamp(0))
            assert (clock.now().run(DefaultModules()) ==
                    datetime_mock.now.return_value)


class TestRandom:
    def test_randint(self):
        with mock.patch('pfun.random.random_.randint') as randint_mock:
            randint_mock.return_value = 1
            assert random.randint(0, 1).run(DefaultModules()) == 1

    def test_random(self):
        with mock.patch('pfun.random.random_.random') as random_mock:
            random_mock.return_value = 0.5
            assert random.random().run(DefaultModules()) == 0.5
