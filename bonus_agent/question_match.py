"""Conservative deduplication of pending questions; never infer a financial answer."""
import re
import unicodedata
from difflib import SequenceMatcher


def same_pending(question, text, related):
    if question.answer or question.withdrawn:
        return False
    if question.related and related and question.related != related:
        return False
    norm=lambda s: re.sub(r'[^\w]', '', unicodedata.normalize('NFKC',s)).casefold()
    a,b=norm(question.text),norm(text)
    # Short questions, changed numeric premises, or changed quoted fields are not safely equivalent.
    if min(len(a),len(b))<40:
        return a==b
    if set(re.findall(r'\d+(?:\.\d+)?',question.text)) != set(re.findall(r'\d+(?:\.\d+)?',text)):
        return False
    fields=lambda s: {norm(x) for x in re.findall(r'[「“]([^」”]+)[」”]',s)}
    if fields(question.text)!=fields(text):
        return False
    return SequenceMatcher(None,a,b,autojunk=False).ratio()>=.90
