# Solver rule types

The solver knows these rule **types**. The actual rules (which teachers, which numbers, which dates)
are data: `rules/*.json` today, the Supabase `rules` table later. A rule is applied only when it is
`active`, inside `valid_from`–`valid_to`, and `confirmed` by the admin.

Built in, not rules: a teacher is in one place at a time; a section has one thing per period;
a lab room holds one class at a time; covers come only from teachers who already teach that section;
a section never gets the same lab twice in a day.

| `rule_type` | Kind | Params | Effect |
|---|---|---|---|
| `lock_subjects` | hard | `subjects` | Lectures of these subjects never move |
| `lock_teachers` | hard | teachers | These teachers' lectures never move |
| `available_window` | hard/temporary | `from_period` or `periods`, optional `days` | Teacher unavailable outside the window |
| `no_extra_lectures` | hard | teachers | Never used as a cover |
| `max_lectures_per_day` | hard | `max`, `extra_allowed`, `extra_penalty{science,arts}` | Cap per teacher; lectures above `max` cost more (arts more than science) |
| `consecutive_limit` | hard | `max` | Never more than `max` lectures in a row within a block (between breaks) |
| `avoid_consecutive` | soft | `max`, weight | Penalise runs longer than `max` |
| `max_extra_per_section` | hard | `max` | Covers per teacher per section per day |
| `sports_limit` | hard | `per_lecture_per_year`, `per_section_per_day` | Sports caps |
| `avoid_sports_period` | soft | `numbers`, weight | Penalise sports in these lecture numbers |
| `lab_daily_cap` | hard | `per_type` | Labs per lab type per day |
| `lab_needs_incharge` | hard | — | An absent lab incharge closes that lab |
| `extra_subject_priority` | soft | `groups{GROUP:[subjects]}`, `last`, `bonus`, `malus` | Which subject's teacher is preferred for a cover |
| `science_not_last` | soft | weight | Penalise science moved into the last lecture |
| `shuffle_penalty` | soft | `per_move` | Enables shuffling; cost per moved lecture (absent = no shuffling) |
| `allow_rotation_shuffle` | soft | — | The day-tagged rotation cell may also move |
| `fill_costs` | soft | `year`, `costs{cover,lab,sports,counsellor}` | Order of options for an empty slot, per year |
| `counsellors` | hard | teachers, `max_per_day` | Student counsellors may cover, up to the cap |

Anything that cannot be filled under these rules becomes `! FIX` for the admin, never a silent clash.
