{
  description = "Development environment for geo-report extraction and analysis.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            bash
            git
            jq
            poppler-utils
            uv
          ];

          shellHook = ''
            export GEO_REPORT_ROOT="$PWD"
            export PEACE_TOOL_POOL_REPO="''${PEACE_TOOL_POOL_REPO:-https://github.com/Lexikat-Pte-Ltd/peace-tool-pool.git}"
            export PEACE_TOOL_POOL_REF="''${PEACE_TOOL_POOL_REF:-main}"
            export PEACE_TOOL_POOL_ROOT="''${PEACE_TOOL_POOL_ROOT:-$PWD/.cache/peace-tool-pool}"
            export GEOMAP_CACHE_ROOT="''${GEOMAP_CACHE_ROOT:-$PWD/output/.geomap-cache}"
            export GEOMAP_DATA_ROOT="''${GEOMAP_DATA_ROOT:-$PEACE_TOOL_POOL_ROOT/data}"
            export GEOMAP_MODEL_ROOT="''${GEOMAP_MODEL_ROOT:-$PEACE_TOOL_POOL_ROOT/dependencies/models}"
            export GEOMAP_KNOWLEDGE_ROOT="''${GEOMAP_KNOWLEDGE_ROOT:-$PEACE_TOOL_POOL_ROOT/dependencies/knowledge}"
            export GEOMAP_MCP_ALLOWED_ROOTS="''${GEOMAP_MCP_ALLOWED_ROOTS:-$PWD/output:$PEACE_TOOL_POOL_ROOT/dependencies:$PEACE_TOOL_POOL_ROOT/data}"
          '';
        };
      });
}
