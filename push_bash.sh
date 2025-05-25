#!/usr/bin/env bash
# push_bash.sh — push current branch to origin
set -e
sudo git add .
sudo git commit -m "${1:-update}"
sudo git push -u origin "$(git rev-parse --abbrev-ref HEAD)"
