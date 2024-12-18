import argparse
import json
import pathlib
import re
import os
import sys
from pathlib import Path
import subprocess
import shutil
import hashlib
import logging

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - R.CLCTR - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

UNKNOWN = "UNKNOWN"
RESULTS_OK = "/tmp/check_results_ok"
# Note 1: it would be better to add lshw, dmidecode, and other tools to the image
# to collect more detailed information about the system.
# Note 2: script will work only on Debian-like systems


def capture_env(cmd):
    """Executes a command using subprocess and captures environment variables.
    :param cmd: Command string to execute.
    :return: A dictionary of the environment variables with their values.
    """
    # Note: It's assumed that shell is bash!
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, shell=True, executable="/bin/bash"
    )
    output, _ = proc.communicate()
    env_lines = output.decode().split("\n")
    capture = {}
    for line in env_lines:
        if "=" in line:
            var_name, var_value = line.split("=", 1)
            capture[var_name] = var_value
    return capture


def collect_vars(c_path, iterations):
    """Sources the config file and extracts only the variables that were exported.
    :param c_path: Path to the configuration file
    :param iterations: Number of iterations for MMTests
    :return: A dict of the sourced env vars
    """
    pre_env = capture_env("env")
    post_env = capture_env(f"source {c_path} && env")
    # Find diff
    exported_variables = {
        key: value
        for key, value in post_env.items()
        if key not in pre_env or value != pre_env.get(key)
    }
    exported_variables["MMTESTS_ITERATIONS"] = iterations
    exported_variables["MMTESTS_CONFIG"] = c_path.stem

    return exported_variables


def run_command(command):
    """Run a shell command and return its output as a string."""
    try:
        out = subprocess.check_output(
            command, shell=True, stderr=subprocess.STDOUT
        ).decode("utf-8")
        return out.strip()
    except subprocess.CalledProcessError as e:
        log.error("%s, exit status: %s", e.cmd, e.returncode)
        return ""


def parse_cpu_info():
    """Parse CPU information from lscpu command."""
    cpu_info = run_command("lscpu")
    caches = {
        line.split(":")[0]: line.split(":")[1].strip()
        for line in cpu_info.splitlines()
        if "cache" in line.lower()
    }
    match = re.search(r"CPU MHz:\s+(\S+)", cpu_info)
    freq = match.group(1) if match else UNKNOWN
    return {
        "Arch": re.search(r"Architecture:\s+(\S+)", cpu_info).group(1),
        "Cores": int(re.search(r"^CPU\(s\):\s+(\d+)", cpu_info, re.MULTILINE).group(1)),
        "Frequency": f"{freq} MHz",
        "Caches": caches,
    }


def parse_memory_info():
    """Parse memory information from /proc/meminfo."""
    mem_info = run_command("grep MemTotal /proc/meminfo")
    total = int(re.search(r"\d+", mem_info).group(0)) // 1024
    return {
        "Total": f"{total} MB",
        "Speed": UNKNOWN,
    }


def get_instance_type():
    """Get the instance type from the environment variable"""
    return os.getenv("INSTANCE_TYPE", "UNKNOWN")


def parse_storage_info():
    """Parse storage information from lsblk command."""
    # Remove header and split lines
    block_info = run_command("lsblk -b -o NAME,SIZE,TYPE,MOUNTPOINT").splitlines()[1:]
    disks = []
    current_disk = {}

    for line in block_info:
        parts = line.split()
        name = parts[0]

        # Remove Unicode characters
        name = re.sub(r"[\u2500-\u257F]", "", name)
        size = f"{int(parts[1]) // (1024 ** 3)}G"
        block_type = parts[2]
        mountpoint = parts[3] if len(parts) > 3 else "Not mounted"

        if type == "disk":
            if current_disk:
                disks.append(current_disk)
            current_disk = {"Name": name, "Size": size, "Partitions": []}
        elif block_type == "part" and current_disk:
            partition = {"Name": name, "Size": size, "Mountpoint": mountpoint}
            current_disk["Partitions"].append(partition)

    if current_disk:
        disks.append(current_disk)
    return {"Disks": disks}


def parse_os_info():
    """Parse OS information from /etc/os-release."""
    os_info = UNKNOWN
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME"):
                    os_info = line.split("=")[1].strip().strip('"')
                    break
    except Exception:
        os_info = "error retrieving OS information"

    return {
        "Name": os_info,
        "Packages list": get_installed_packages(),
    }


def get_installed_packages():
    """Get a list of installed packages"""
    output = subprocess.check_output(["dpkg", "-l"], text=True)
    packages = {}
    patt = re.compile(r"^ii\s+(\S+)\s+(\S+)")
    for line in output.splitlines():
        match = patt.match(line)
        if match:
            package_name, version = match.groups()
            packages[package_name] = version
    return packages


def parse_kernel_info():
    """Parse kernel version"""
    kernel_version = run_command("uname -r")
    return {
        "Version": kernel_version,
        "SHA256": collect_sha256_kernel(kernel_version),
    }


def parse_filesystem_info():
    """Parse filesystem information from df command."""
    # Skip header
    df_info = run_command("df -Th").splitlines()[1:]
    fses = []
    for line in df_info:
        name, fs_type, size, used, avail, use_perc, mounted_on = line.split(None, 6)
        # Filter out loop and tmpfs
        if "loop" not in name and "tmpfs" not in fs_type:
            fs = {
                "Name": name,
                "Type": fs_type,
                "Size": size,
                "Used": used,
                "Avail": avail,
                "Use%": use_perc.strip("%"),
                "Mounted on": mounted_on,
            }
            fses.append(fs)
    return {"Filesystems": fses}


def get_file_sha256(file_path):
    """Calculate the SHA256 hash of a file"""
    sha256_hash = hashlib.sha256()
    block_size = 4096
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(block_size), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_current_kernel_loc(ver):
    """Get the location of the current kernel binary"""
    loc = f"/boot/vmlinuz-{ver}"
    if os.path.exists(loc):
        return loc


def collect_sha256_kernel(ver):
    """Collect the SHA256 hash of the current kernel"""
    loc = get_current_kernel_loc(ver)
    if loc:
        sha256 = get_file_sha256(loc)
        return sha256
    return "UNKNOWN"


def read_sha256_file(file_path):
    """Read the SHA256 hash from a file"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            sha256 = line.split()[0]
            return sha256
    except IOError:
        log.error("Unable to read %s", file_path)
        return "UNKNOWN"


def collect_sha256_benchmark(cfg_name):
    """Collect the SHA256 hash of the benchmark"""
    loc = f"/mmtests/{cfg_name}.SHA256"
    if pathlib.Path(loc).exists():
        return read_sha256_file(loc)
    else:
        log.warning("Unable to find file: %s", loc)


def if_cmd_exists(cmd):
    if not shutil.which(cmd):
        log.warning("%s command is not available", cmd)
        return False
    return True


def parse_boottime():
    """Parse the system boot time."""
    time_patt = re.compile(r"(\d+(?:\.\d+)?)(ms|us|s)")

    def parse_time(t):
        match = time_patt.match(t)
        if not match:
            return 0
        value, unit = match.groups()
        value = float(value)
        if unit == "ms":
            return int(value)
        if unit == "us":
            return int(value / 1000)
        return int(value * 1000)

    blame_info = {}
    time_info = {}

    if not if_cmd_exists("systemd-analyze"):
        log.warning("System boot time information is not available")
        return {"blame": blame_info, "time": time_info}

    try:
        blame_output = run_command("systemd-analyze blame")
        if blame_output:
            blame_info = {
                name: parse_time(time_str)
                for line in blame_output.splitlines()
                if line.strip()
                for time_str, name in [line.split(maxsplit=1)]
            }
    except Exception as e:
        log.error("Parsing blame output:", e)

    try:
        time_output = run_command("systemd-analyze time")
        if time_output:
            lines = time_output.splitlines()
            if lines:
                startup_parts = lines[0].split()
                time_info = {
                    "kernel": float(startup_parts[3][:-1]),
                    "userspace": float(startup_parts[6][:-1]),
                    "total": float(startup_parts[9][:-1]),
                }

                if len(lines) > 1:
                    graphical_target_parts = lines[1].split()
                    time_info["graphical_target"] = float(
                        graphical_target_parts[3][:-1]
                    )
    except Exception as e:
        log.error("Parsing time output: %s", e)

    return {"blame": blame_info, "time": time_info}


def collect_system_info(cfg_name):
    """Build a dictionary with system information."""
    return {
        "CPU": parse_cpu_info(),
        "Memory": parse_memory_info(),
        "Storage": parse_storage_info(),
        "OS": parse_os_info(),
        "Kernel": parse_kernel_info(),
        "Filesystem": parse_filesystem_info(),
        "Instance type": get_instance_type(),
        "Benchmark SHA256": collect_sha256_benchmark(cfg_name),
        "Boot time": parse_boottime(),
    }


def mmtest_extract_json(benchmark, r_root, c_name, extractor):
    """Extracts benchmark results in JSON format.
    :param benchmark: Benchmark name
    :param r_root: The root directory where the results are stored
    :param c_name: The name of the MMTests config file
    :param extractor: Path to the extract-mmtests.pl script
    """
    command = f"{extractor} -d {r_root} -b {benchmark} -n {c_name} --print-json"
    json_output = run_command(command)
    results_data = json.loads(json_output)

    if results_data:
        return results_data

    log.error("results data for %s", benchmark)
    return None


def check_results(results_data):
    """Checks the extracted JSON results for specific conditions."""
    errors = False

    if "_OperationsSeen" not in results_data:
        log.error("_OperationsSeen is not present in the results data")
        errors = True

    if len(results_data.get("_OperationsSeen", {})) == 0:
        log.error("_OperationsSeen is empty")
        errors = True

    if "_ResultData" not in results_data:
        log.error("_ResultData is not present in the results data")
        errors = True

    if len(results_data.get("_ResultData", {}).keys()) == 0:
        log.error("_ResultData is empty")
        errors = True

    return errors


def get_results_root(test_dir):
    """Get the results directory from the test directory"""
    result = Path(test_dir) / "work/log"
    if not result.is_dir():
        log.error("results dir %s does not exist", result)
        raise FileNotFoundError
    return result


def get_names(target_dir):
    """Get the names of the benchmarks from the results directory
    :param target_dir: The directory where the results are stored
    :return: A list of benchmark names
    """
    result = []
    for root, _, _ in os.walk(f"{target_dir}/iter-0"):
        if root.endswith("logs"):
            match = re.search(r"iter-0/(.+)/logs", root)
            if match:
                result.append(match.group(1))
    return result


def compose_filename(benchmark, c_name):
    """Compose output name for JSON file"""
    return f"BENCHMARK{benchmark}_CONFIG{c_name}.json"


def collect_times(r_dir: Path):
    """Collect start and finish times for each test"""
    result = {}

    patterns = {
        "start": re.compile(r"start :: (\d+)"),
        "finish": re.compile(r"finish :: (\d+)"),
        "test_begin": re.compile(r"test begin :: \w+ (\d+)"),
        "test_end": re.compile(r"test end :: \w+ (\d+)"),
    }

    for item in r_dir.iterdir():
        if item.is_dir():
            iter_data = {}
            times_file = item / "tests-timestamp"
            if times_file.exists():
                with times_file.open("r") as f:
                    file_contents = f.read()
                    for key, pattern in patterns.items():
                        match = pattern.search(file_contents)
                        if match:
                            iter_data[key] = int(match.group(1))
            if iter_data:
                result[item.name] = iter_data
    return result


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Collect SUT information and environment vars"
    )
    parser.add_argument(
        "-o",
        metavar="OUTPUT_DIR",
        required=True,
        help="Output dir path",
    )
    parser.add_argument(
        "-c",
        metavar="MMTESTS_CONFIG_FILE",
        required=True,
        help="Path to MMTests config file, e.g. configs/config-name",
    )
    parser.add_argument(
        "-d", metavar="TEST_DIR", required=True, help="Specify test directory"
    )
    parser.add_argument(
        "-i",
        metavar="MMTEST_ITERATIONS",
        type=int,
        required=True,
        help="Number of iterations for MMTests",
    )
    parser.add_argument("-f", action="store_true", help="Collect full archive")
    result = parser.parse_args()

    test_dir = Path(result.d)
    if not test_dir.exists():
        log.error("TEST_DIR %s does not exist", result.d)
        sys.exit(1)

    c_path = test_dir / result.c
    if not c_path.exists():
        log.error("MMTESTS_CONFIG_FILE %s does not exist", c_path)
        sys.exit(1)

    return result


if __name__ == "__main__":
    args = parse_args()

    mmtest_extr = f"{args.d}/bin/extract-mmtests.pl"
    config_path = Path(args.d) / Path(args.c)
    config_name = config_path.stem
    output_dir = Path(args.o)

    # This is global info
    variables = collect_vars(config_path, args.i)
    info = collect_system_info(config_name)

    results_root = get_results_root(args.d)
    results_dir = results_root / config_name

    if not results_dir.is_dir():
        log.error("results dir '%s' does not exist", results_dir)
        sys.exit(1)

    # Clean up the results check file after previous run
    try:
        os.remove(RESULTS_OK)
    except FileNotFoundError:
        pass

    if args.f:
        try:
            shutil.copytree(results_dir, output_dir / results_dir.stem)
            log.info("full results dir collected in %s", output_dir)
        except FileNotFoundError:
            log.error("the results directory does not exist")

    times = collect_times(results_dir)

    benchmarks = get_names(results_dir)
    log.info("benchmarks detected: %s", ", ".join(benchmarks))

    for bench in benchmarks:
        output_file = compose_filename(bench, config_name)
        output_path = output_dir / output_file
        results = mmtest_extract_json(bench, results_root, config_name, mmtest_extr)

        if check_results(results):
            log.error("results check failed for %s", bench)
            sys.exit(1)
        else:
            log.info("results check passed for %s", bench)
            with open(RESULTS_OK, "w", encoding="utf-8") as file:
                pass

        data = {
            "variables": variables,
            "sys_info": info,
            "results": results,
            "times": times,
        }

        with open(output_path, "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, indent=2)
            log.info("results collected to %s", output_path)
