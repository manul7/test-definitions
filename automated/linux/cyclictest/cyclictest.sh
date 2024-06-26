#!/bin/sh
# cyclictest measures event latency in Linux kernel by measuring the amount of
# time that passes between when a timer expires and when the thread which set
# the timer actually runs.

# shellcheck disable=SC1091
. ../../lib/sh-test-lib

OUTPUT="$(pwd)/output"
LOGFILE="${OUTPUT}/cyclictest.json"
RESULT_FILE="${OUTPUT}/result.txt"

PRIORITY="98"
INTERVAL="1000"
THREADS="1"
AFFINITY="0"
DURATION="1m"
BACKGROUND_CMD=""
HISTOGRAM=""

usage() {
    echo "Usage: $0 [-p priority] [-i interval] [-t threads] [-a affinity] [-D duration ] [-h max_latency ] [-w background_cmd]" 1>&2
    exit 1
}

while getopts ":p:i:t:a:D:h:w:" opt; do
    case "${opt}" in
        p) PRIORITY="${OPTARG}" ;;
        i) INTERVAL="${OPTARG}" ;;
        t) THREADS="${OPTARG}" ;;
        a) AFFINITY="${OPTARG}" ;;
        D) DURATION="${OPTARG}" ;;
        h) HISTOGRAM="${OPTARG}" ;;
        w) BACKGROUND_CMD="${OPTARG}" ;;
        *) usage ;;
    esac
done

! check_root && error_msg "Please run this script as root."
create_out_dir "${OUTPUT}"

if [ -n "${HISTOGRAM}" ]; then
    HISTOGRAM="-h ${HISTOGRAM}"
else
    HISTOGRAM=""
fi

# Run cyclictest.
if ! binary=$(command -v cyclictest); then
    detect_abi
    # shellcheck disable=SC2154
    binary="./bin/${abi}/cyclictest"
fi

background_process_start bgcmd --cmd "${BACKGROUND_CMD}"

# shellcheck disable=SC2086
"${binary}" -q -p "${PRIORITY}" -i "${INTERVAL}" -t "${THREADS}" -a "${AFFINITY}" \
    -D "${DURATION}" ${HISTOGRAM} -m --json="${LOGFILE}"

background_process_stop bgcmd

# Parse test log.
../../lib/parse_rt_tests_results.py cyclictest "${LOGFILE}" \
    | tee -a "${RESULT_FILE}"
