
from collections import deque
from typing import Deque, List, Optional
from pydantic import BaseModel, Field
from .constants import (
    API_READ_TIMEOUT, LATENCY_DEQUE_LEN,
    ErrorCodes, Strategy, err_msg
)

class Status(BaseModel):
    """Status protocol consists of models' information."""
    models: Optional[List[str]] = Field(default=[], examples=[[]])
    unfinished: int = 0
    latency: Deque = Field(default=deque(maxlen=LATENCY_DEQUE_LEN),
                           examples=[[]])
    speed: Optional[int] = Field(default=None, examples=[None])
    avaiaible: Optional[bool] = Field(default=True, examples=[False])
    api_key: Optional[str] = Field(default=None, examples=[None])
    # The api_key is used to access the node, if the node requires
    health_check: Optional[bool] = Field(default=None, examples=[True])
    # The health_check is used to check the node's health


class Node(BaseModel):
    """Node protocol consists of url and status."""
    url: str
    status: Optional[Status] = None

class ErrorResponse(BaseModel):
    """Error responses."""
    message: str
    type: str
    code: int
    param: Optional[str] = None
    object: str = 'error'

