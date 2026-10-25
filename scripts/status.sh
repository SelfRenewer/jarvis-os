#!/usr/bin/env bash
# Human-readable snapshot: services, today's reminders, recent activity. Reads the database only.
set -euo pipefail
cd "$(dirname "$0")/.."
TZNAME="${DEFAULT_USER_TIMEZONE:-Asia/Tbilisi}"
q() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-jarvis}" -d "${POSTGRES_DB:-jarvis}" -At -F ' | ' -c "$1"; }
echo "== Сервисы"
docker compose ps --format '  {{.Service}}: {{.Status}}'
echo
echo "== Ближайшие напоминания"
q "SELECT '  '||to_char(fire_at AT TIME ZONE '$TZNAME','DD.MM HH24:MI'), CASE channel WHEN 'call' THEN 'звонок' ELSE 'telegram' END,
          coalesce(payload->>'summary','') FROM reminders WHERE status='scheduled' ORDER BY fire_at LIMIT 10" || true
echo
echo "== Отправлено за сутки"
q "SELECT '  '||to_char(sent_at AT TIME ZONE '$TZNAME','DD.MM HH24:MI'), left(replace(text, E'\n', ' '), 90)
     FROM notifications WHERE status='sent' AND sent_at > now() - interval '24 hours' ORDER BY sent_at DESC LIMIT 10" || true
echo
echo "== Данные"
q "SELECT '  задач Todoist: '||(SELECT count(*) FROM todoist_tasks WHERE NOT checked AND NOT is_deleted)
       ||' · событий календаря (2 нед.): '||(SELECT count(*) FROM calendar_events WHERE status<>'cancelled' AND start_at > now())
       ||' · идей: '||(SELECT count(*) FROM ideas)
       ||' · список покупок: '||(SELECT count(*) FROM shopping_items WHERE status='open')"
echo
echo "== Последняя синхронизация"
q "SELECT '  '||provider||': '||to_char(last_success_at AT TIME ZONE '$TZNAME','DD.MM HH24:MI')
     FROM integration_sync_state WHERE provider IN ('todoist','google_calendar') ORDER BY provider"
echo
echo "== Фоновые задачи за сутки"
q "SELECT '  '||status||': '||count(*) FROM scheduled_jobs WHERE updated_at > now() - interval '24 hours' GROUP BY status ORDER BY status"
dead="$(q "SELECT count(*) FROM scheduled_jobs WHERE status='dead' AND finished_at > now() - interval '24 hours'")"
if [[ "$dead" != "0" ]]; then
  echo
  echo "== Ошибки (упавшие задачи за сутки)"
  q "SELECT '  '||to_char(finished_at AT TIME ZONE '$TZNAME','DD.MM HH24:MI'), job_type, left(coalesce(last_error,''), 100)
       FROM scheduled_jobs WHERE status='dead' AND finished_at > now() - interval '24 hours' ORDER BY finished_at DESC LIMIT 5"
fi
