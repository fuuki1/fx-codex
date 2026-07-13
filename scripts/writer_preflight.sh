#!/bin/zsh
# Shared fail-closed writer/crontab predicates for install and restart preflights.

FX_WRITER_PATTERN='fx_briefing\.py|fx_tf_snapshot\.py|fx_(briefing|tf_snapshot)_loop\.sh|fx_briefing_once\.sh|run_exclusive\.py.*fx-(briefing|snapshot)'

fx_crontab_is_absent() {
  local exit_code="$1"
  local error_file="$2"
  local line_count
  [ "$exit_code" -eq 1 ] || return 1
  line_count=$(awk 'END { print NR }' "$error_file") || return 1
  [ "$line_count" -eq 1 ] || return 1
  grep -Eq '^(crontab: )?no crontab for [[:alnum:]_.-]+$' "$error_file"
}

fx_filter_writer_lines() {
  grep -E "$FX_WRITER_PATTERN"
}

fx_file_has_writer_signature() {
  local candidate_file="$1"
  fx_filter_writer_lines < "$candidate_file" >/dev/null
}
