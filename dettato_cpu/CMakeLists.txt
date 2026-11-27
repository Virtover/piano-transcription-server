# dettato_server (server.cpp) against dettato.cpp, for ensemble jobs on the CPU. Built by Dockerfile.worker:
#   cmake -S dettato_cpu -B build -DDETTATO_CPP_DIR=/src/dettato.cpp [-DGGML_AVX2=OFF ...]
# dettato.cpp fetches its pinned ggml (v1.0.0) at configure time.
cmake_minimum_required(VERSION 3.24)
project(dettato_server LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(DETTATO_CPP_DIR "" CACHE PATH "a checkout of dettato.cpp")
if(NOT DETTATO_CPP_DIR)
    message(FATAL_ERROR "set DETTATO_CPP_DIR")
endif()

set(DETTATO_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(DETTATO_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(DETTATO_VULKAN OFF CACHE BOOL "" FORCE)
set(DETTATO_METAL OFF CACHE BOOL "" FORCE)
add_subdirectory(${DETTATO_CPP_DIR}/cpp dettato)

add_executable(dettato_server server.cpp)
target_link_libraries(dettato_server PRIVATE dettato_ggml)
# One file that runs on any Linux of the same architecture: the worker image's glibc is older than the build image's.
target_link_options(dettato_server PRIVATE -static)
