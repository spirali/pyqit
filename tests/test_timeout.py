from datetime import timedelta

from testutils import init
init()

import haydi as hd  # noqa


def test_maxall_timeout():
    r = hd.Range(1000000)

    def fn(x):
        return sum(xrange(x * x))

    result = r.map(fn).max(lambda x: x).run(timeout=timedelta(seconds=3))
    assert result is not None
    assert result > 0
