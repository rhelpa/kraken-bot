# Backup branch name in case you need to roll back
sudo git branch before-clean
# Rewrite history to delete the file containing the secret
sudo git filter-branch --force \
  --index-filter "git rm --cached --ignore-unmatch push_bash.sh" \
  --prune-empty --tag-name-filter cat -- --all
# Clean up:
sudo rm -rf .git/refs/original/
sudo git reflog expire --expire=now --all
sudo git gc --prune=now --aggressive
# Force-push the sanitized history
sudo git push origin --force --all
