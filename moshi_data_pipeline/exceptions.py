class PipelineError(RuntimeError):
    """Base class for actionable pipeline failures."""


class ConfigurationError(PipelineError):
    """Invalid or incompatible configuration."""


class ExternalToolError(PipelineError):
    """An external executable returned an error."""


class InputValidationError(PipelineError):
    """The source media is missing or unsuitable."""


class DependencyError(PipelineError):
    """An optional runtime dependency is unavailable."""


class ModelStageError(PipelineError):
    """A model-backed stage failed."""


class QualityError(PipelineError):
    """An artifact failed a mandatory quality check."""


class UnsupportedFeatureError(PipelineError):
    """An intentionally unsupported experimental feature was requested."""
