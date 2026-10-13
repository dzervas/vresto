"""Observe pending native fetch/XHR without intercepting browser networking."""


class NativeLoadObservation:
    """Track only requests begun within this observation's lifetime."""

    def __init__(self, page):
        self.page = page
        self.pending = set()
        page.on('request', self._requested)
        page.on('requestfinished', self._completed)
        page.on('requestfailed', self._completed)

    def _requested(self, request):
        if request.resource_type in ('fetch', 'xhr'):
            self.pending.add(request)

    def _completed(self, request):
        self.pending.discard(request)

    def close(self):
        self.page.remove_listener('request', self._requested)
        self.page.remove_listener('requestfinished', self._completed)
        self.page.remove_listener('requestfailed', self._completed)
