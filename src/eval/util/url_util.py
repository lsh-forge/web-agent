import re
import urllib.parse
import codecs
from urllib.parse import urlparse, parse_qs, urlunparse

# 全局配置（可根据场景调整）
STOPWORDS = {
    'com', 'org', 'net', 'gov', 'edu', 'cn', 'uk', 'us', 'jp', 'kr',
    'html', 'htm', 'php', 'jsp', 'asp', 'cgi', 'aspx', 'do', 'action',
    'www', 'http', 'https', 'ftp', 'sftp', 'mailto', 'file',
    'index', 'home', 'default', 'main', 'page', 'view', 'show'
}
REDUNDANT_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                    'session_id', 'token', 'timestamp', 'cache', 'version'}
KEEP_SEPARATORS = r'[-_:/?&=]'

def is_url(text: str) -> bool:
    pattern = r'%[0-9a-fA-F]{2}'
    if re.match(pattern, text):
        return True
    return False


def url_decode(url: str) -> str:
    """
    URL 解码
    """
    return urllib.parse.unquote(url)
