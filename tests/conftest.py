"""Load the protocol package without importing Home Assistant setup code."""

from pathlib import Path
import sys
from types import ModuleType

package_path = Path(__file__).resolve().parents[1] / "custom_components" / "nuvo_m3"
package = ModuleType("custom_components.nuvo_m3")
package.__path__ = [str(package_path)]
sys.modules[package.__name__] = package
