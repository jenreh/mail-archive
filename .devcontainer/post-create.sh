#!/bin/bash
set -eu

: "${WORKSPACE:?WORKSPACE environment variable must be set}"
cd "$WORKSPACE"
# Configure git to allow the workspace as a safe directory
git config --global --add safe.directory "$WORKSPACE"

echo "Setting up zsh"
mkdir -p /home/vscode/.mcp
mkdir -p /home/vscode/.antigen/bundles/robbyrussell/oh-my-zsh/cache/completions
cp "$WORKSPACE"/.devcontainer/.p10k.zsh "$HOME"/.p10k.zsh
cp "$WORKSPACE"/.devcontainer/.zshrc "$HOME"/.zshrc

echo "Initializing project"
task init
