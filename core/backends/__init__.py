"""发布后端包。

契约见 base.py。注册表见 registry.py。
新增后端：实现 base.PublishBackend 后，在 registry.REGISTRY 登记即可。
"""
from .base import BackendError, Job, PublishBackend, PublishResult  # noqa: F401
from .registry import REGISTRY, describe, get  # noqa: F401
