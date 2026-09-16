from datetime import time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

CameraDevice = Annotated[str, Field(pattern=r"^(/dev/(video[0-9]+|v4l/by-id/[A-Za-z0-9_.+-]+)|gphoto2:(usb:[0-9]{3},[0-9]{3}|serial:[0-9a-f]{64}))$", max_length=255)]
FrameId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]

class Settings(BaseModel):
    name: str = Field(default="My plant study", min_length=1, max_length=80)
    camera_device: CameraDevice = "/dev/video0"
    interval_minutes: int = Field(default=30, ge=1, le=10080)
    duration_days: int = Field(default=90, ge=1, le=1095)
    width: int = Field(default=1280, ge=320, le=16384, multiple_of=2)
    height: int = Field(default=720, ge=240, le=16384, multiple_of=2)
    input_format: Literal["auto", "mjpeg", "yuyv422", "uyvy422", "nv12", "nv21", "rgb24", "bgr24", "gray", "yuv420p", "h264"] = "auto"
    autofocus: bool = True
    focus_settle_seconds: int = Field(default=3, ge=1, le=15)
    warmup_seconds: int = Field(default=2, ge=0, le=10)
    timezone: str = "UTC"
    daylight_only: bool = False
    day_start: str = "07:00"
    day_end: str = "19:00"
    reserve_mb: int = Field(default=1024, ge=100, le=1048576)
    export_fps: int = Field(default=24, ge=1, le=60)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone, such as America/Toronto")
        return value

    @field_validator("day_start", "day_end")
    @classmethod
    def valid_time(cls, value):
        try:
            parsed = time.fromisoformat(value)
            if parsed.tzinfo or len(value) != 5:
                raise ValueError()
        except ValueError:
            raise ValueError("Use HH:MM in 24-hour time")
        return value

    @model_validator(mode="after")
    def distinct_window(self):
        if self.daylight_only and self.day_start == self.day_end:
            raise ValueError("Capture window start and end must differ")
        return self


class PreviewRequest(BaseModel):
    settings: Settings | None = None


class FrameSelection(BaseModel):
    frame_ids: list[FrameId] = Field(min_length=1, max_length=1000)
    excluded: bool


class ExportRequest(BaseModel):
    cutoff: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    normalize_lighting: bool = False
