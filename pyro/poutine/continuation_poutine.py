from __future__ import absolute_import, division, print_function

from .poutine import Messenger, Poutine


class ContinuationMessenger(Messenger):
    """
    TODO docs
    """
    def __init__(self, escape_fn, cont_fn, first_available_dim):
        """
        TODO docs
        """
        self.escape_fn = escape_fn
        self.cont_fn = cont_fn
        self.first_available_dim = first_available_dim
        self.next_available_dim = None

    def __enter__(self):
        """
        TODO docs
        """
        self.next_available_dim = self.first_available_dim
        return super(ContinuationMessenger, self).__enter__()

    def _pyro_sample(self, msg):
        """
        TODO docs
        """
        if self.escape_fn(msg) and not msg["done"]:
            msg["done"] = True
            msg["continuation"] = self.cont_fn


class ContinuationPoutine(Poutine):
    def __init__(self, fn, escape_fn, cont_fn, first_available_dim):
        super(ContinuationPoutine, self).__init__(
            ContinuationMessenger(escape_fn, cont_fn, first_available_dim), fn)
