"""Deterministic external dependency simulator for success/timeout/failure demos."""
class DependencyError(Exception):
    transient = True

class PermanentDependencyError(DependencyError):
    transient = False

class ExternalDependencySimulator:
    def verify(self, dependency_name: str, mode: str = "success", attempt_number: int = 1) -> dict:
        if mode == "transient_then_success":
            if attempt_number == 1: raise DependencyError(f"{dependency_name} temporary simulated outage")
            return {"dependency": dependency_name, "status": "SUCCESS", "reference": f"sim-{dependency_name}-verified-after-retry"}
        if mode == "timeout": raise DependencyError(f"{dependency_name} timed out")
        if mode == "failure": raise DependencyError(f"{dependency_name} is temporarily unavailable")
        if mode == "permanent_failure": raise PermanentDependencyError(f"{dependency_name} rejected the verification")
        if mode != "success": raise PermanentDependencyError(f"Unsupported simulation mode: {mode}")
        return {"dependency": dependency_name, "status": "SUCCESS", "reference": f"sim-{dependency_name}-verified"}
