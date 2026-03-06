
from typing import List

from fastapi import Request
from openaiproxy.services.deps import get_settings_service

def get_request_id(request: Request) -> str:
    """
    从请求头中获取请求ID，如果存在的话
    """
    return request.headers.get("x-request-id", None)

def get_gateway_id(request: Request) -> str:
    """
    从请求头中获取网关ID，如果存在的话
    """
    return request.headers.get("x-gateway-id", None)

def is_request_via_gateway(request: Request) -> bool:
    """
    判断请求是否来自于网关（如Viagateway）
    可以通过检查特定的请求头或其他标识来确定
    这里网关会添加一个特定的请求头 "x-gateway-id" 来标识请求来自网关
    """
    return bool(
        "x-gateway-id" in request.headers and \
        get_gateway_id(request)
    )

def get_request_app_id(request: Request) -> str:
    """
    从请求头中获取调用放方应用ID，如果存在的话
    """
    return request.headers.get("x-app-id", None)

def get_self_api_id(request: Request) -> str:
    """
    从请求头中获取当前接口的API ID，如果存在的话
    """
    return request.headers.get("x-api-id", None)

def get_request_user_id(request: Request) -> str:
    """
    从请求头中获取用户ID，如果存在的话
    """
    return request.headers.get("x-credential-userid", None)

def get_request_user_name(request: Request) -> str:
    """
    从请求头中获取用户名，如果存在的话
    """
    inheader_username = None
    if "iv-user" in request.headers:
        inheader_username = request.headers.get("iv-user", None)
    if not inheader_username:
        return inheader_username
    return request.headers.get("x-credential-username", None)

def get_request_display_name(request: Request) -> str:
    """
    从请求头中获取用户显示名称，如果存在的话
    """
    return request.headers.get("x-credential-displayname", None)

def is_app_request(request: Request) -> bool:
    """
    判断请求是否来自于应用（如通过Viagateway转发的请求）
    可以通过检查特定的请求头或其他标识来确定
    这里应用会添加一个特定的请求头 "x-app-id" 来标识请求来自应用
    """
    return bool(
        "x-app-id" in request.headers and \
        get_request_app_id(request)
    )

def get_request_app_groups(request: Request) -> List[str]:
    """
    从请求头中获取调用方应用所属的用户组列表，如果存在的话
    """
    app_groups_str = request.headers.get("x-app-groups", "")
    if app_groups_str:
        return [group.strip() for group in app_groups_str.split(",")]
    return []

def get_protocol_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的协议类型（如HTTP、WebSocket等），如果存在的话
    """
    return request.headers.get(
        "x-forwarded-proto",
        request.url.scheme
    )

def get_client_user_agent(request: Request) -> str:
    """
    从请求头中获取用户代理信息，如果存在的话
    """
    return request.headers.get("user-agent", None)

def get_uri_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的原始URI，如果存在的话
    """
    return request.headers.get(
        "x-source-uri", request.url.path
    )

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 阿里DNS
        s.connect(("223.5.5.5", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        return "127.0.0.1"

def get_host_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的原始Host信息，如果存在的话
    """
    gateway_host_name = request.headers.get("x-host-override", None) or \
                        request.headers.get("x-forwarded-host", None) or \
                        request.headers.get("host", request.url.hostname)
    if gateway_host_name:
        return gateway_host_name.split(":")[0]  # 去掉端口号
    return get_local_ip()

def get_port_via_gateway(request: Request) -> int:
    """
    从请求头中获取通过网关转发的原始端口信息，如果存在的话
    """
    http_port = request.url.port or get_settings_service().settings.port
    gateway_port = request.headers.get(
        "x-forwarded-port", str(http_port)
    )
    return int(gateway_port)

def get_trace_parent_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的Trace ID，如果存在的话
    """
    return request.headers.get("x-trace-parent", None)

def get_trace_chain_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的Trace Chain信息，如果存在的话
    """
    return request.headers.get("x-app-sticky", None)

def build_publish_url_via_gateway(request: Request, target_url: str) -> str:
    """
    构建通过网关转发的完整URL地址
    """

    if target_url.startswith("https://") or target_url.startswith("http://"):
        return target_url

    proto = get_protocol_via_gateway(request) or "http"
    host = get_host_via_gateway(request)
    port = get_port_via_gateway(request)

    if target_url.startswith("//"):
        return f"{proto}:{target_url}"

    if (proto == "https" and port == 443) or (
        proto == "http" and port == 80
    ) or (proto == "ws" and port == 80) or (
        proto == "wss" and port == 443
    ):
        return f"{proto}://{host}{target_url}"
    else:
        return f"{proto}://{host}:{port}{target_url}"

def build_websocket_url_via_gateway(request: Request, target_url: str) -> str:
    """
    构建通过网关转发的完整URL地址
    """

    if target_url.startswith("https://") or target_url.startswith("http://"):
        if target_url.startswith("https://"):
            return target_url.replace("https://", "wss://", 1)
        else:
            return target_url.replace("http://", "ws://", 1)
    elif target_url.startswith("ws://") or target_url.startswith("wss://"):
        return target_url

    proto = get_protocol_via_gateway(request) or "ws"
    host = get_host_via_gateway(request)
    port = get_port_via_gateway(request)

    if proto == "https":
        proto = "wss"
    elif proto == "http":
        proto = "ws"

    if target_url.startswith("//"):
        return f"{proto}:{target_url}"

    if (proto == "https" and port == 443) or (
        proto == "http" and port == 80
    ) or (proto == "ws" and port == 80) or (
        proto == "wss" and port == 443
    ):
        return f"{proto}://{host}{target_url}"
    else:
        return f"{proto}://{host}:{port}{target_url}"

def get_client_real_ip_via_gateway(request: Request) -> str:
    """
    从请求头中获取通过网关转发的客户端IP地址，如果存在的话
    // 获得真实IP
    String realIp = request.getRemoteAddr();
    String clientIp = request.getHeader("X-Forwarded-For");
    if (!StringUtils.isEmpty(clientIp)) {
        realIp = clientIp;
    } else {
        clientIp = request.getHeader("X-Real-IP");
        if (!StringUtils.isEmpty(clientIp)) {
            realIp = clientIp;
        }
    }
    return StringUtils.split(realIp, ",")[0];
    """
    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    # 如果没有x-forwarded-for头，尝试获取x-real-ip头
    x_real_ip = request.headers.get("x-real-ip", "")
    ip_address = x_forwarded_for or x_real_ip

    if ip_address:
        # x-forwarded-for可能包含多个IP地址，取第一个非空的IP地址
        for ip in ip_address.split(","):
            ip = ip.strip()
            if ip:
                return ip
    # 最后返回请求的远程地址
    return request.client.host if request.client else None
