from typing import Any


def type_filter(requests: list) -> list[Any]:
    """
    判断 URL 是否在指定域名下
    :param url: URL
    :param base_domain: 域名
    :return: True/False
    """
    filtered_requests = []
    for request in requests:
        resource_type = request.get("resource_type")
        if resource_type not in ["fetch", "xhr"]:
            continue
        filtered_requests.append(request)



    return filtered_requests