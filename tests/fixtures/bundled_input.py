"""Line input for the account-free POSIX PTY fixture."""
import codecs
import re


def read_utf8_lines(read):
    decoder = codecs.getincrementaldecoder('utf-8')()
    pending = ''
    while True:
        raw = read()
        if not raw:
            decoder.decode(b'', final=True)
            return
        pending += decoder.decode(raw)
        while re.search(r'[\r\n]', pending):
            end = re.search(r'[\r\n]', pending).start()
            line, pending = pending[:end], pending[end + 1:]
            yield line
