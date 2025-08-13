"""Fast Universal Tessdata Path Finder for all operating systems."""
import os
import shutil
import subprocess
import platform
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Set
import threading
import time

try:
    import winreg
except ImportError:
    winreg = None

class TessdataFinder:
    """Fast, robust tessdata directory finder for all operating systems."""
    
    def __init__(self, timeout: float = 10.0, max_workers: int = 4):
        """
        Initialize the tessdata finder.
        
        Args:
            timeout: Maximum time to spend searching (seconds)
            max_workers: Maximum number of concurrent search threads
        """
        self.timeout = timeout
        self.max_workers = max_workers
        self.start_time = None
        self.found_paths: Set[str] = set()
        self.lock = threading.Lock()
        self.os_type = platform.system().lower()
        
    def find_all_tessdata_paths(self, verbose: bool = False) -> List[str]:
        """
        Find all tessdata directory paths on the system.
        
        Args:
            verbose: If True, prints search progress
            
        Returns:
            List of tessdata directory paths, ordered by priority
        """
        self.start_time = time.time()
        self.found_paths.clear()
        
        if verbose:
            print(f"Searching for tessdata paths on {self.os_type}...")
        
        # Priority-ordered search methods
        search_methods = [
            self._check_environment_vars,
            self._check_tesseract_binary_info,
            self._check_common_locations,
            self._check_registry_windows if self.os_type == 'windows' else self._dummy_method,
            self._search_filesystem_smart,
        ]
        
        # Execute high-priority methods first (sequentially)
        for method in search_methods[:3]:
            if self._is_timeout():
                break
            try:
                paths = method(verbose)
                with self.lock:
                    self.found_paths.update(paths)
            except Exception as e:
                if verbose:
                    print(f"Warning: {method.__name__} failed: {e}")
        
        # Execute remaining methods concurrently
        if not self._is_timeout() and len(search_methods) > 3:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(method, verbose): method.__name__ 
                          for method in search_methods[3:]}
                
                for future in as_completed(futures, timeout=max(1, self.timeout - self._elapsed())):
                    if self._is_timeout():
                        break
                    try:
                        paths = future.result(timeout=1)
                        with self.lock:
                            self.found_paths.update(paths)
                    except Exception as e:
                        if verbose:
                            print(f"Warning: {futures[future]} failed: {e}")
        
        # Sort by priority and filter valid paths
        result = self._prioritize_paths(list(self.found_paths))
        
        if verbose:
            print(f"Found {len(result)} tessdata paths in {self._elapsed():.2f}s")
            
        return result
    
    def find_primary_tessdata_path(self, verbose: bool = False) -> Optional[str]:
        """
        Get the primary (best) tessdata directory path.
        
        Args:
            verbose: If True, prints search progress
            
        Returns:
            Primary tessdata path, None if not found
        """
        paths = self.find_all_tessdata_paths(verbose)
        return paths[0] if paths else None
    
    def _check_environment_vars(self, verbose: bool = False) -> List[str]:
        """Check environment variables for tessdata paths."""
        paths = []
        env_vars = ['TESSDATA_PREFIX', 'TESSERACT_DATA_PATH', 'TESSERACT_PREFIX']
        
        for var in env_vars:
            path = os.environ.get(var)
            if path:
                # Handle both direct tessdata path and parent directory
                candidates = [path, os.path.join(path, 'tessdata')]
                for candidate in candidates:
                    if self._is_valid_tessdata_dir(candidate):
                        paths.append(os.path.abspath(candidate))
                        if verbose:
                            print(f"Found via ENV {var}: {candidate}")
                        break
        
        return paths
    
    def _check_tesseract_binary_info(self, verbose: bool = False) -> List[str]:
        """Get tessdata paths from tesseract binary information."""
        paths = []
        
        # Find tesseract binaries
        binaries = self._find_tesseract_binaries()
        
        for binary in binaries:
            if self._is_timeout():
                break
                
            # Method 1: Ask tesseract directly
            try:
                result = subprocess.run(
                    [binary, '--print-parameters'],
                    capture_output=True, text=True, timeout=3
                )
                for line in result.stdout.split('\n'):
                    if 'tessdata' in line.lower():
                        # Extract path-like strings
                        words = line.split()
                        for word in words:
                            if 'tessdata' in word and os.path.isdir(word):
                                if self._is_valid_tessdata_dir(word):
                                    paths.append(os.path.abspath(word))
                                    if verbose:
                                        print(f"Found via tesseract info: {word}")
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
            
            # Method 2: Relative to binary location
            try:
                bin_dir = Path(binary).parent.resolve()
                relative_paths = self._get_relative_tessdata_paths()
                
                for rel_path in relative_paths:
                    candidate = bin_dir / rel_path
                    if candidate.is_dir() and self._is_valid_tessdata_dir(str(candidate)):
                        abs_path = str(candidate.resolve())
                        if abs_path not in paths:
                            paths.append(abs_path)
                            if verbose:
                                print(f"Found relative to binary: {abs_path}")
            except Exception:
                continue
        
        return paths
    
    def _check_common_locations(self, verbose: bool = False) -> List[str]:
        """Check common installation locations."""
        paths = []
        common_paths = self._get_common_tessdata_paths()
        
        for path_template in common_paths:
            if self._is_timeout():
                break
                
            try:
                # Expand environment variables and user paths
                expanded_path = os.path.expandvars(os.path.expanduser(path_template))
                if os.path.isdir(expanded_path) and self._is_valid_tessdata_dir(expanded_path):
                    abs_path = os.path.abspath(expanded_path)
                    paths.append(abs_path)
                    if verbose:
                        print(f"Found in common location: {abs_path}")
            except Exception:
                continue
        
        return paths
    
    def _check_registry_windows(self, verbose: bool = False) -> List[str]:
        """Check Windows registry for tessdata paths."""
        paths = []
        
        if not winreg or self.os_type != 'windows':
            return paths
        
        registry_locations = [
            (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Tesseract-OCR"),
            (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Tesseract-OCR"),
            (winreg.HKEY_CURRENT_USER, "SOFTWARE\\Tesseract-OCR"),
        ]
        
        for hkey, subkey in registry_locations:
            try:
                with winreg.OpenKey(hkey, subkey) as key:
                    install_path, _ = winreg.QueryValueEx(key, "InstallPath")
                    tessdata_path = os.path.join(install_path, "tessdata")
                    if self._is_valid_tessdata_dir(tessdata_path):
                        paths.append(os.path.abspath(tessdata_path))
                        if verbose:
                            print(f"Found via registry: {tessdata_path}")
            except (FileNotFoundError, OSError, PermissionError):
                continue
        
        return paths
    
    def _search_filesystem_smart(self, verbose: bool = False) -> List[str]:
        """Smart filesystem search with optimization."""
        paths = []
        
        if self._is_timeout():
            return paths
        
        search_roots = self._get_search_roots()
        
        with ThreadPoolExecutor(max_workers=min(len(search_roots), self.max_workers)) as executor:
            futures = {
                executor.submit(self._search_directory_tree, root, verbose): root
                for root in search_roots
            }
            
            for future in as_completed(futures, timeout=max(1, self.timeout - self._elapsed())):
                if self._is_timeout():
                    break
                try:
                    found_paths = future.result(timeout=1)
                    paths.extend(found_paths)
                except Exception:
                    continue
        
        return paths
    
    def _search_directory_tree(self, root_path: str, verbose: bool = False) -> List[str]:
        """Search directory tree for tessdata folders."""
        found = []
        max_depth = 4 if self.os_type == 'windows' else 5
        
        def _recursive_search(current_path: Path, depth: int):
            if depth > max_depth or self._is_timeout():
                return
            
            try:
                if current_path.name == 'tessdata' and self._is_valid_tessdata_dir(str(current_path)):
                    abs_path = str(current_path.resolve())
                    found.append(abs_path)
                    if verbose:
                        print(f"Found via filesystem search: {abs_path}")
                    return
                
                # Only search relevant subdirectories
                if self._should_search_directory(current_path):
                    for child in current_path.iterdir():
                        if child.is_dir() and not child.is_symlink():
                            _recursive_search(child, depth + 1)
                        
            except (PermissionError, OSError, FileNotFoundError):
                pass
        
        try:
            _recursive_search(Path(root_path), 0)
        except Exception:
            pass
        
        return found
    
    def _find_tesseract_binaries(self) -> List[str]:
        """Find tesseract binary locations."""
        binaries = []
        
        # Method 1: Use shutil.which (most reliable)
        for name in ['tesseract', 'tesseract.exe']:
            binary = shutil.which(name)
            if binary and binary not in binaries:
                binaries.append(binary)
        
        # Method 2: Check PATH explicitly
        path_env = os.environ.get('PATH', '')
        for path_dir in path_env.split(os.pathsep):
            if not path_dir:
                continue
            for name in ['tesseract', 'tesseract.exe']:
                binary_path = os.path.join(path_dir, name)
                if (os.path.isfile(binary_path) and 
                    os.access(binary_path, os.X_OK) and 
                    binary_path not in binaries):
                    binaries.append(binary_path)
        
        # Method 3: Common locations
        common_binary_paths = self._get_common_binary_paths()
        for path in common_binary_paths:
            expanded = os.path.expandvars(os.path.expanduser(path))
            if (os.path.isfile(expanded) and 
                os.access(expanded, os.X_OK) and 
                expanded not in binaries):
                binaries.append(expanded)
        
        return binaries
    
    def _get_relative_tessdata_paths(self) -> List[str]:
        """Get possible relative paths from binary to tessdata."""
        if self.os_type == 'windows':
            return [
                'tessdata',
                '../tessdata', 
                '../../tessdata',
                '../share/tessdata',
                '../share/tesseract-ocr/tessdata'
            ]
        else:
            return [
                '../share/tesseract-ocr/tessdata',
                '../share/tesseract/tessdata',
                '../share/tessdata',
                '../../share/tesseract-ocr/tessdata',
                '../../share/tesseract/tessdata',
                '../tessdata',
                'tessdata'
            ]
    
    def _get_common_tessdata_paths(self) -> List[str]:
        """Get common tessdata installation paths by OS."""
        if self.os_type == 'windows':
            return [
                "C:/Program Files/Tesseract-OCR/tessdata",
                "C:/Program Files (x86)/Tesseract-OCR/tessdata", 
                "C:/Tesseract-OCR/tessdata",
                "C:/tools/tesseract/tessdata",
                "${LOCALAPPDATA}/Tesseract-OCR/tessdata",
                "${PROGRAMFILES}/Tesseract-OCR/tessdata",
                "${PROGRAMFILES(X86)}/Tesseract-OCR/tessdata",
                "~/AppData/Local/Tesseract-OCR/tessdata",
                "~/tessdata"
            ]
        elif self.os_type == 'darwin':  # macOS
            return [
                "/usr/local/share/tesseract-ocr/tessdata",
                "/usr/local/share/tessdata", 
                "/opt/homebrew/share/tesseract-ocr/tessdata",
                "/usr/share/tesseract-ocr/tessdata",
                "~/.local/share/tesseract/tessdata",
                "~/tessdata"
            ]
        else:  # Linux and others
            return [
                "/usr/share/tesseract-ocr/tessdata",
                "/usr/share/tessdata",
                "/usr/local/share/tesseract-ocr/tessdata", 
                "/usr/local/share/tessdata",
                "/opt/tesseract/share/tessdata",
                "~/.local/share/tesseract/tessdata",
                "~/.tesseract/tessdata",
                "~/tessdata"
            ]
    
    def _get_common_binary_paths(self) -> List[str]:
        """Get common binary paths by OS."""
        if self.os_type == 'windows':
            return [
                "C:/Program Files/Tesseract-OCR/tesseract.exe",
                "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
                "C:/tools/tesseract/tesseract.exe"
            ]
        else:
            return [
                "/usr/bin/tesseract",
                "/usr/local/bin/tesseract",
                "/opt/tesseract/bin/tesseract"
            ]
    
    def _get_search_roots(self) -> List[str]:
        """Get filesystem search root directories."""
        if self.os_type == 'windows':
            # Get available drives
            drives = []
            for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
                drive = f"{letter}:\\"
                if os.path.exists(drive):
                    drives.append(drive)
            
            roots = []
            for drive in drives[:2]:  # Limit to first 2 drives for speed
                roots.extend([
                    os.path.join(drive, "Program Files"),
                    os.path.join(drive, "Program Files (x86)"),
                ])
            return [r for r in roots if os.path.exists(r)]
        else:
            return ["/usr", "/usr/local", "/opt"]
    
    def _should_search_directory(self, path: Path) -> bool:
        """Determine if directory should be searched."""
        name = path.name.lower()
        
        # Skip system/uninteresting directories
        skip_dirs = {
            'windows', 'system32', 'syswow64', '$recycle.bin', 'recovery',
            'proc', 'sys', 'dev', 'run', 'tmp', 'var/tmp', 'boot',
            '.git', '.svn', '__pycache__', 'node_modules', '.vscode'
        }
        
        if name in skip_dirs:
            return False
        
        # Focus on directories likely to contain tesseract
        interesting_keywords = {
            'tesseract', 'ocr', 'program', 'share', 'local', 'opt', 'tools', 'bin'
        }
        
        return any(keyword in name for keyword in interesting_keywords) or len(name) < 3
    
    def _is_valid_tessdata_dir(self, path: str) -> bool:
        """Check if directory contains tessdata files."""
        try:
            path_obj = Path(path)
            if not path_obj.is_dir():
                return False
            
            # Look for .traineddata files
            has_traineddata = any(f.suffix == '.traineddata' for f in path_obj.iterdir())
            return has_traineddata
        except (OSError, PermissionError):
            return False
    
    def _prioritize_paths(self, paths: List[str]) -> List[str]:
        """Sort paths by priority/preference."""
        def priority_key(path: str) -> tuple:
            path_lower = path.lower()
            
            # Higher priority (lower number) for:
            priority = 0
            
            # Environment variable paths
            if any(env in path_lower for env in ['tessdata_prefix', 'tesseract']):
                priority -= 1000
            
            # Standard system locations
            if any(sys_path in path_lower for sys_path in ['/usr/share', '/usr/local/share', 'program files']):
                priority -= 100
                
            # Prefer paths with version info (usually more recent)
            if any(char.isdigit() for char in path):
                priority -= 10
                
            return (priority, path_lower)
        
        # Remove duplicates while preserving order
        seen = set()
        unique_paths = []
        for path in paths:
            normalized = os.path.normpath(path)
            if normalized not in seen:
                seen.add(normalized)
                unique_paths.append(normalized)
        
        return sorted(unique_paths, key=priority_key)
    
    def _elapsed(self) -> float:
        """Get elapsed time since search started."""
        return time.time() - self.start_time if self.start_time else 0
    
    def _is_timeout(self) -> bool:
        """Check if search has timed out."""
        return self._elapsed() > self.timeout
    
    def _dummy_method(self, verbose: bool = False) -> List[str]:
        """Dummy method for OS-specific methods not applicable."""
        return []


# Convenience functions for backward compatibility
def get_tessdata_paths(verbose: bool = False, timeout: float = 10.0) -> List[str]:
    """
    Find all tessdata directory paths on the system.
    
    Args:
        verbose: If True, prints search progress
        timeout: Maximum search time in seconds
        
    Returns:
        List of tessdata directory paths
    """
    finder = TessdataFinder(timeout=timeout)
    return finder.find_all_tessdata_paths(verbose=verbose)


def get_primary_tessdata_path(verbose: bool = False, timeout: float = 10.0) -> Optional[str]:
    """
    Get the primary (best) tessdata directory path.
    
    Args:
        verbose: If True, prints search progress
        timeout: Maximum search time in seconds
        
    Returns:
        Primary tessdata path, None if not found
    """
    finder = TessdataFinder(timeout=timeout)
    return finder.find_primary_tessdata_path(verbose=verbose)


# Example usage and testing
if __name__ == "__main__":
    print(f"Fast Tessdata Finder - Running on: {platform.system()}")
    print(f"Python: {sys.version}")
    print("-" * 50)
    
    # Quick search
    start_time = time.time()
    primary_path = get_primary_tessdata_path(verbose=True, timeout=5.0)
    search_time = time.time() - start_time
    
    print(f"\nPrimary tessdata path: {primary_path}")
    print(f"Search completed in: {search_time:.2f} seconds")
    
    # Comprehensive search
    print("\n" + "="*50)
    print("Comprehensive search:")
    start_time = time.time()
    all_paths = get_tessdata_paths(verbose=True, timeout=10.0)
    search_time = time.time() - start_time
    
    print(f"\nAll tessdata paths found ({len(all_paths)}):")
    for i, path in enumerate(all_paths, 1):
        print(f"{i:2d}. {path}")
    
    print(f"\nTotal search time: {search_time:.2f} seconds")
    
    # Verify paths
    print(f"\nPath verification:")
    for path in all_paths:
        try:
            files = [f for f in os.listdir(path) if f.endswith('.traineddata')]
            print(f"  {path}: {len(files)} .traineddata files")
        except Exception as e:
            print(f"  {path}: Error reading - {e}")
