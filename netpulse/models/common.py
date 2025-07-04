from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class QueueStrategy(str, Enum):
    FIFO = "fifo"
    PINNED = "pinned"


class DriverName(str, Enum):
    NAPALM = "napalm"
    NETMIKO = "netmiko"
    PYEAPI = "pyeapi"
    # NCCLIENT = "ncclient"
    # PURESNMP = "puresnmp"
    # RESTCONF = "restconf"


class JobAdditionalData(BaseModel):
    """
    Used in rq.Job.meta.
    We can store custom data here.
    """

    error: Optional[Tuple[str, str]] = None  # 0: exc_type, 1: exc_value


class JobResult(BaseModel):
    """
    A customized version of `rq.job.Result`.
    """

    class ResultType(int, Enum):
        SUCCESSFUL = 1
        FAILED = 2
        STOPPED = 3
        RETRIED = 4

    type: ResultType
    retval: Optional[Any] = None
    error: Optional[Any] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "type": 1,
                "retval": "Interface GigabitEthernet1/0/1",
                "error": {
                    "type": "ValueError",
                    "message": "Something went wrong",
                },
            }
        }
    )


class NodeInfo(BaseModel):
    hostname: str
    count: int
    capacity: int
    queue: str

    def __hash__(self):
        return hash(self.hostname)

    def __eq__(self, value):
        return self.hostname == value.hostname


class WebHook(BaseModel):
    class WebHookMethod(str, Enum):
        GET = "GET"
        POST = "POST"
        PUT = "PUT"
        DELETE = "DELETE"
        PATCH = "PATCH"

    name: str = Field("basic", description="Name of the WebHookCaller")
    url: HttpUrl = Field(..., description="Webhook URL")
    method: WebHookMethod = Field(WebHookMethod.POST, description="HTTP method for webhook")

    headers: Optional[Dict[str, str]] = Field(None, description="Custom headers for the request")
    cookies: Optional[Dict[str, str]] = Field(None, description="Cookies to send with the request")
    auth: Optional[Tuple[str, str]] = Field(None, description="(Username, Password) for Basic Auth")
    timeout: float = Field(
        5.0, ge=0.5, le=120.0, description="Request timeout in seconds (default 5s)"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "basic",
                "url": "http://localhost:5000/webhook",
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "timeout": 5.0,
            }
        }
    )


class DriverConnectionArgs(BaseModel):
    """
    NOTE: We loose the field checking to Optional.
    Strict checks should be done in derived classes.
    """

    device_type: Optional[str] = Field(None, description="Device type")
    host: Optional[str] = Field(None, description="Device IP address")
    username: Optional[str] = Field(None, description="Device username")
    password: Optional[str] = Field(None, description="Device password")

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "example": {
                "device_type": "cisco_ios",
                "host": "172.17.0.1",
                "username": "admin",
                "password": "admin",
            }
        },
    )

    def enforced_field_check(self):
        """
        DriverConnectionArgs could be auto-filled in Batch APIs.
        After that, we need to manually check.
        """
        if self.host is None:
            raise ValueError("host is None")

        return self


class DriverArgs(BaseModel):
    """
    This is a generic model for driver arguments.
    Depends on the driver's method, the arguments can be different.
    """

    model_config = ConfigDict(extra="allow")
