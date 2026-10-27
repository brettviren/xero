# Super-build of a xero topic's CMake sources.
#
# Installed by 'xero init' as <area>/share/superbuild/CMakeLists.txt and
# symlinked as topics/<t>/source/CMakeLists.txt.  A topic may replace its
# symlink with a real (modified) copy.
#
# The packages under this directory depend on each other through
# `find_package(<pkg> CONFIG REQUIRED)`, i.e. each one expects its in-tree
# siblings to already be *installed*.  So every package is configured, built
# and installed into one shared prefix via ExternalProject_Add, in dependency
# order.
#
# The inter-package dependency graph is DISCOVERED: a dependency on a sibling
# is a find_package() name matching another package's project() name.
#
# xero drives this with:
#   XERO_INIT_CACHE  an initial-cache script (cmake -C) holding every setting
#                    for the sub-builds: install prefix, CMAKE_PREFIX_PATH,
#                    compilers, launchers, RPATH and topic/env -D options.
#   XERO_INIT_HASH   hash of that file's content, so a changed setting
#                    re-runs each sub-build's configure step.
#   XERO_EXCLUDE     subdirectory names not to build (eg not in this env).
#
# Sub-packages are built with Ninja when available.  BUILD_ALWAYS re-invokes
# every sub-build so source edits are always picked up; the sub-build's own
# timestamp checks keep that cheap.
cmake_minimum_required(VERSION 3.21)
project(xero_superbuild LANGUAGES NONE)

include(ExternalProject)

if(NOT XERO_INIT_CACHE)
  message(FATAL_ERROR "xero super: configure with 'xero build' (XERO_INIT_CACHE unset)")
endif()

find_program(XERO_NINJA NAMES ninja ninja-build)
if(XERO_NINJA)
  set(XERO_SUB_GENERATOR "Ninja")
else()
  set(XERO_SUB_GENERATOR "${CMAKE_GENERATOR}")
endif()
message(STATUS "xero super: sub-build generator = ${XERO_SUB_GENERATOR}")

# --- Discover packages and their declared dependencies -----------------------

file(GLOB _entries RELATIVE "${CMAKE_CURRENT_SOURCE_DIR}"
     "${CMAKE_CURRENT_SOURCE_DIR}/*")
list(SORT _entries)

set(XERO_PKGS "")
foreach(_dir IN LISTS _entries)
  set(_cml "${CMAKE_CURRENT_SOURCE_DIR}/${_dir}/CMakeLists.txt")
  if(NOT IS_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/${_dir}")
    continue()
  endif()
  if(_dir IN_LIST XERO_EXCLUDE OR NOT EXISTS "${_cml}")
    continue()
  endif()

  file(STRINGS "${_cml}" _proj_lines REGEX "^[ \t]*[pP][rR][oO][jJ][eE][cC][tT][ \t]*\\(")
  set(_name "")
  foreach(_line IN LISTS _proj_lines)
    if(_line MATCHES "\\([ \t]*([A-Za-z0-9_]+)")
      set(_name "${CMAKE_MATCH_1}")
      break()
    endif()
  endforeach()
  if(NOT _name)
    message(WARNING "xero super: no project() found in ${_cml}, skipping")
    continue()
  endif()

  file(STRINGS "${_cml}" _fp_lines REGEX "^[ \t]*find_package[ \t]*\\(")
  set(_reqs "")
  foreach(_line IN LISTS _fp_lines)
    if(_line MATCHES "^[ \t]*find_package[ \t]*\\([ \t]*([A-Za-z0-9_]+)")
      list(APPEND _reqs "${CMAKE_MATCH_1}")
    endif()
  endforeach()

  list(APPEND XERO_PKGS "${_name}")
  # Build from the resolved source path so topics sharing a worktree also share
  # ccache hits and compile_commands paths are real.
  get_filename_component(XERO_${_name}_SRC "${CMAKE_CURRENT_SOURCE_DIR}/${_dir}" REALPATH)
  set(XERO_${_name}_REQS "${_reqs}")
endforeach()

# --- Register each package, then wire dependencies ---------------------------

foreach(_name IN LISTS XERO_PKGS)
  ExternalProject_Add(${_name}
    PREFIX            "${CMAKE_BINARY_DIR}/${_name}"
    SOURCE_DIR        "${XERO_${_name}_SRC}"
    CMAKE_GENERATOR   "${XERO_SUB_GENERATOR}"
    CMAKE_ARGS        -C "${XERO_INIT_CACHE}" "-DXERO_INIT_HASH=${XERO_INIT_HASH}"
    BUILD_ALWAYS      TRUE
    USES_TERMINAL_CONFIGURE TRUE
    USES_TERMINAL_BUILD     TRUE
    USES_TERMINAL_INSTALL   TRUE
  )
endforeach()

foreach(_name IN LISTS XERO_PKGS)
  set(_internal "")
  foreach(_req IN LISTS XERO_${_name}_REQS)
    if(_req IN_LIST XERO_PKGS AND NOT _req STREQUAL _name)
      list(APPEND _internal "${_req}")
    endif()
  endforeach()
  if(_internal)
    add_dependencies(${_name} ${_internal})
    message(STATUS "xero super: ${_name} depends on ${_internal}")
  else()
    message(STATUS "xero super: ${_name} (no in-tree dependencies)")
  endif()
endforeach()
