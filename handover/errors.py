"""交接服务的领域异常。"""


class HandoverError(Exception):
    """全部交接服务异常的基类。"""


class ValidationError(HandoverError):
    """凭据格式或跨文件引用不合法。"""


class PermissionDenied(HandoverError):
    """提交方对该凭据或操作没有权限。"""


class RuleViolation(HandoverError):
    """违反交接业务规则（如重复卸货、已隔离舱放行）。"""


class NotFound(HandoverError):
    """引用的运输段、舱位或凭据不存在。"""


class AlreadyFinalized(HandoverError):
    """凭据已被双方认可或仲裁定稿，不能再改。"""


class ContradictoryEvidence(HandoverError):
    """有效实称合计超过共同总重，该组凭据必须重新裁定。"""
