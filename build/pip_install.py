"""pip install for an image build, from the next package index when one fails.

    python3 pip_install.py <pip install arguments>
    python3 pip_install.py --torch cu128 torch==2.11.0 torchaudio==2.11.0

The first form takes packages from PyPI or a full mirror of it, the second
from PyTorch's index for that CUDA build or a mirror of it. An index is left
for the next one only when pip's failure may be the index's: not for a
conflict between the requirements or a package that does not compile, which
every index would fail the same way.

Unlike the apt archives and the checksummed downloads, an index's packages
are not signed: a mirror is trusted as much as PyPI. Only mirrors run by
universities and large companies, used widely, are listed, and only after
the original fails. Only the Python standard library.
"""

import subprocess
import sys

# PyPI, then full mirrors of it, each a list of index URLs for pip.
PYPI = [
    # PyPI itself, on Fastly's worldwide network
    ["https://pypi.org/simple"],
    # Kakao, South Korea
    ["https://mirror.kakao.com/pypi/simple"],
    # Alibaba Cloud, China, with its worldwide network
    ["https://mirrors.aliyun.com/pypi/simple"],
    # Tsinghua University, China
    ["https://pypi.tuna.tsinghua.edu.cn/simple"],
]


def torch_indexes(build: str) -> list[list[str]]:
    """PyTorch's index for a CUDA build such as cu128, then mirrors of it."""
    return [
        # PyTorch's own, on CloudFront's worldwide network
        [f"https://download.pytorch.org/whl/{build}"],
        # Nanjing University, China
        [f"https://mirrors.nju.edu.cn/pytorch/whl/{build}"],
        # Shanghai Jiao Tong University, China: it lacks some of torch's
        # dependencies (triton, sympy), which come from a PyPI mirror. A
        # torch from there would be a CUDA build too: pip prefers 2.11.0+cu128
        # to 2.11.0, and the Dockerfiles check torch's CUDA version.
        [f"https://mirror.sjtu.edu.cn/pytorch-wheels/{build}", "https://pypi.tuna.tsinghua.edu.cn/simple"],
    ]


# In pip's output: failures that no other index would change, and that would
# only download everything again.
NOT_THE_INDEX = ("ResolutionImpossible", "Failed to build", "Failed building wheel",
                 "No space left on device", "MemoryError")


def index_arguments(urls: list[str]) -> list[str]:
    return ["--index-url", urls[0], *[part for url in urls[1:] for part in ("--extra-index-url", url)]]


def run_pip(arguments: list[str]) -> tuple[int, str]:
    """Run pip, showing its output as it comes; its exit code and output."""
    process = subprocess.Popen([sys.executable, "-m", "pip", "install", *arguments],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    lines = []
    for line in process.stdout:
        sys.stdout.write(line)
        lines.append(line)
    return process.wait(), "".join(lines)


def install(arguments: list[str], indexes: list[list[str]], run=run_pip) -> int:
    for number, urls in enumerate(indexes):
        code, output = run(index_arguments(urls) + arguments)
        if code == 0:
            return 0
        if any(marker in output for marker in NOT_THE_INDEX):
            return code
        if number + 1 < len(indexes):
            print(f"pip_install: pip failed with {urls[0]}; trying {indexes[number + 1][0]}", file=sys.stderr, flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    indexes = PYPI
    if arguments[:1] == ["--torch"]:
        indexes = torch_indexes(arguments[1])
        arguments = arguments[2:]
    if any(argument.startswith(("--index-url", "--extra-index-url", "-i")) for argument in arguments):
        print("pip_install: the indexes are this script's; give none", file=sys.stderr)
        return 2
    return install(arguments, indexes)


if __name__ == "__main__":
    sys.exit(main())
