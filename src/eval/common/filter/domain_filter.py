from typing import Any
import tldextract


def get_root_domain(url):
    # 解析域名（自动处理协议、路径、子域名）
    extracted = tldextract.extract(url)
    # 处理空域名（如无效URL）
    return extracted.domain

def domain_filter(requests: list, url: str) -> list[Any]:
    """
    判断 URL 是否在指定域名下
    :param url: URL
    :param base_domain: 域名
    :return: True/False
    """
    filtered_requests = []
    base_domain = get_root_domain(url)
    print(f"Base domain {base_domain}")
    for request in requests:
        url = request.get("url")
        root_domain = get_root_domain(url)
        if not root_domain:
            continue
        if root_domain == base_domain:
            filtered_requests.append(request)


    return filtered_requests