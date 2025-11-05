
from fastapi import APIRouter, Request
from fastapi.openapi.docs import (
    get_swagger_ui_html, get_redoc_html
)

router = APIRouter(tags=["自定义接口文档"])

@router.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request):
    app = request.app
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"接口文档 - {app.title}",
        swagger_js_url="/js/swagger-ui-bundle.js",
        swagger_css_url="/css/swagger-ui.css",
        swagger_favicon_url="/favicon.ico",
        init_oauth=None,
        swagger_ui_parameters={
            "deepLinking": True,
            "displayRequestDuration": True,
            "docExpansion": "none",
            "operationsSorter": "alpha",
            "filter": True,
            "showExtensions": True,
            "showCommonExtensions": True,
            "tryItOutEnabled": True,
        }
    )

@router.get("/redoc", include_in_schema=False)
async def custom_redoc_ui_html(request: Request):
    app = request.app
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"接口文档 - {app.title}",
        redoc_js_url="/js/redoc.standalone.js",
        with_google_fonts=False,
        redoc_favicon_url="/favicon.ico",
    )
