-- Learning Pulse Live · TA_Looker_Dump
-- Replaces the manual "Looker TA Sessions table" export. One row per TA doubt session
-- (one_to_one_type 11, 20) under DS courses, same logic as Metabase card 13444
-- ("TA sessions Office vs Non Office Hrs"), with the column names the sheet expects.
-- The sync script replaces {{since}} with a date (YYYY-MM-DD) before running it.
-- Database: Newton School (Metabase database id 4)

WITH s0 AS (
    SELECT v.id AS session_id, v.description, v.cancel_reason, v.one_to_one_status,
           v.booked_by_id AS student_user_id, v.booked_with_id AS mentor_user_id,
           v.start_timestamp, v.one_to_one_token_id, c.title AS booked_course
    FROM video_sessions_onetoone v
    JOIN courses_course c ON c.id = v.course_id
    WHERE v.one_to_one_type IN (11, 20)
      AND c.id <> 144
      AND c.course_structure_id IN (11,14,20,26,50,51,52,53,54,55,56,57,58,59,60,61,72,73,127,118,119,122,121,132,95,219,222,223,224,191,192,193,194,269,270)
      AND EXISTS (SELECT 1 FROM courses_courseusermapping cm WHERE cm.course_id = c.id AND cm.status = 8)
      AND v.start_timestamp >= DATE '{{since}}'
),
joins AS (
    SELECT r.one_to_one_id AS session_id,
           MIN(r.join_time)  FILTER (WHERE cm.user_id = s0.student_user_id) AS student_join_time,
           MAX(r.leave_time) FILTER (WHERE cm.user_id = s0.student_user_id) AS student_leave_time,
           MIN(r.join_time)  FILTER (WHERE cm.user_id = s0.mentor_user_id)  AS mentor_join_time,
           MAX(r.leave_time) FILTER (WHERE cm.user_id = s0.mentor_user_id)  AS mentor_leave_time
    FROM video_sessions_onetoonecourseuserreport r
    JOIN s0 ON s0.session_id = r.one_to_one_id
    JOIN courses_courseusermapping cm ON cm.id = r.course_user_mapping_id
    WHERE r.report_type = 4
    GROUP BY 1
),
fb AS (
    SELECT m.entity_object_id AS session_id,
           MAX(CASE WHEN q.feedback_question_id = 535 THEN fa.text END) AS rating,
           MAX(CASE WHEN q.feedback_question_id = 534 THEN fa.text END) AS feedback_given,
           MAX(CASE WHEN fa.id IS NULL AND q.feedback_question_id = 240 THEN q.other_answer END) AS subjective_feedback
    FROM feedback_feedbackformusermapping m
    JOIN feedback_feedbackformuserquestionanswermapping q ON q.feedback_form_user_mapping_id = m.id
    LEFT JOIN feedback_feedbackformuserquestionanswerm2m mm ON mm.feedback_form_user_question_answer_mapping_id = q.id
    LEFT JOIN feedback_feedbackanswer fa ON fa.id = mm.feedback_answer_id
    WHERE m.feedback_form_id IN (4455, 4445)
      AND m.entity_object_id IN (SELECT session_id FROM s0)
    GROUP BY 1
),
topic AS (
    SELECT DISTINCT ON (s0.session_id) s0.session_id, tpt.title AS module_name
    FROM s0
    JOIN video_sessions_onetoonetokentopicmapping tm ON tm.one_to_one_token_id = s0.one_to_one_token_id
    JOIN technologies_topic tt ON tt.id = tm.topic_id
    LEFT JOIN LATERAL (
        SELECT tpt.title FROM technologies_topicnode tn
        JOIN technologies_topictemplate tpt ON tpt.id = tn.topic_template_id
        WHERE tn.topic_id = tt.id AND tpt.id IN (208, 209, 367, 447, 489, 544, 555, 577, 1490, 707, 1749)
        ORDER BY tn.topic_id LIMIT 1
    ) tpt ON TRUE
    ORDER BY s0.session_id, tpt.title NULLS LAST
),
au AS (
    SELECT DISTINCT ON (cum.user_id) cum.user_id, c.title AS au_batch_name
    FROM courses_courseusermapping cum
    JOIN courses_course c ON c.id = cum.course_id
    WHERE cum.status = 8
      AND cum.user_id IN (SELECT student_user_id FROM s0)
      AND c.course_structure_id IN (14,20,26,50,51,52,53,54,55,56,57,58,59,60,61,127,118,119,122,121,156,157,94,95,131,132,191,219,222,223,224,269,270,255,321,329,373,320)
      AND EXISTS (SELECT 1 FROM courses_courseusermapping lu WHERE lu.admin_course_user_mapping_id = cum.id)
    ORDER BY cum.user_id, c.start_timestamp DESC
),
rec AS (
    SELECT DISTINCT ON (video_session_object_id) video_session_object_id AS session_id, recording
    FROM video_sessions_videosessionrecording
    WHERE video_session_object_id IN (SELECT session_id FROM s0)
    ORDER BY video_session_object_id, id DESC
),
hist AS (
    SELECT object_id AS session_id, MAX(action_time) AS action_time
    FROM histories_historyentry
    WHERE content_type_id = 100
      AND object_id IN (SELECT session_id FROM s0)
    GROUP BY 1
)
SELECT
    s0.session_id,
    (s0.start_timestamp AT TIME ZONE 'Asia/Kolkata')                     AS session_start_time,
    s0.student_user_id,
    TRIM(CONCAT(su.first_name, ' ', su.last_name))                       AS student_name,
    COALESCE(mb.module_batch,
             CASE WHEN s0.booked_course LIKE 'Professional Certificate%' THEN 'Not mapped'
                  ELSE s0.booked_course END)                              AS "Batch",
    TRIM(CONCAT(mu.first_name, ' ', mu.last_name))                       AS mentor_name,
    s0.cancel_reason,
    fb.feedback_given,
    fb.subjective_feedback,
    CASE WHEN fb.rating ~ '^[0-9]+(\.[0-9]+)?$' THEN fb.rating::numeric ELSE 0 END AS rating,
    -- Status labels match the old Looker export (and Session_Settings!E4:F31).
    CASE
        WHEN s0.one_to_one_status = 1 THEN 'Pending Confirmation'
        WHEN s0.one_to_one_status = 2 AND j.student_join_time IS NOT NULL AND j.mentor_join_time IS NOT NULL THEN 'Conducted'
        WHEN s0.one_to_one_status = 2 THEN 'Confirmed'
        WHEN s0.one_to_one_status = 3 THEN 'Interviewer Declined'
        WHEN s0.one_to_one_status = 4 THEN 'Interviewer Cancelled'
        WHEN s0.one_to_one_status = 5 THEN 'Student Cancelled'
        WHEN s0.one_to_one_status = 6 THEN 'Lost'
        WHEN s0.one_to_one_status = 7 THEN 'Student pending confirmation'
        WHEN s0.one_to_one_status = 8 THEN 'Interviewer pending confirmation'
        WHEN s0.one_to_one_status = 9 THEN 'Student declined'
        WHEN s0.one_to_one_status = 11 THEN 'Not conducted - Student No show'
        WHEN s0.one_to_one_status = 12 THEN 'Not conducted - Interviewer No show'
        WHEN s0.one_to_one_status = 10 AND (s0.cancel_reason LIKE 'Insufficient time spent by booked by user%'
             OR s0.cancel_reason LIKE 'Insufficient overlap time'
             OR (j.mentor_join_time IS NOT NULL AND j.student_join_time IS NULL)) THEN 'Not conducted - Student No show'
        WHEN s0.one_to_one_status = 10 AND (s0.cancel_reason LIKE 'Insufficient time spent by booked with user%'
             OR (j.mentor_join_time IS NULL AND j.student_join_time IS NOT NULL)) THEN 'Not conducted - Interviewer No show'
        WHEN s0.one_to_one_status = 10 AND j.mentor_join_time IS NULL AND j.student_join_time IS NULL
             THEN 'Not Conducted - No show from Student and Interviewer'
        WHEN s0.one_to_one_status = 10 THEN 'Not Conducted'
    END                                                                   AS session_status,
    CASE WHEN rec.recording IS NOT NULL
         THEN CONCAT('https://my.newtonschool.co/play-video/?url=https://d3dyfaf3iutrxo.cloudfront.net/', rec.recording)
    END                                                                   AS "Lecture_Recording_Link",
    CASE
        WHEN EXTRACT(DOW FROM (s0.start_timestamp AT TIME ZONE 'Asia/Kolkata')) IN (0, 6) THEN 'Non Office hours'
        WHEN NOT ((s0.start_timestamp AT TIME ZONE 'Asia/Kolkata')::time BETWEEN '10:00:00' AND '18:30:00') THEN 'Non Office hours'
        ELSE 'Office hours'
    END                                                                   AS time_category,
    tp.module_name                                                        AS "Module",
    (tok.created_at AT TIME ZONE 'Asia/Kolkata')                          AS booked_time,
    CASE WHEN j.student_leave_time IS NOT NULL AND j.mentor_leave_time IS NOT NULL
         THEN ROUND((EXTRACT(EPOCH FROM (LEAST(j.student_leave_time, j.mentor_leave_time)
                                       - GREATEST(j.student_join_time, j.mentor_join_time))) / 60)::numeric, 5)
    END                                                                   AS over_lap_time_in_minute,
    s0.description,
    au.au_batch_name,
    NULL::text                                                            AS slot_opened_time,  -- not used by the sheet
    (j.student_join_time AT TIME ZONE 'Asia/Kolkata')                     AS student_join_time,
    (j.mentor_join_time AT TIME ZONE 'Asia/Kolkata')                      AS mentor_join_time,
    (hist.action_time AT TIME ZONE 'Asia/Kolkata')                        AS action_time
FROM s0
LEFT JOIN joins j   ON j.session_id = s0.session_id
LEFT JOIN fb        ON fb.session_id = s0.session_id
LEFT JOIN topic tp  ON tp.session_id = s0.session_id
LEFT JOIN au        ON au.user_id = s0.student_user_id
LEFT JOIN rec       ON rec.session_id = s0.session_id
LEFT JOIN hist      ON hist.session_id = s0.session_id
LEFT JOIN auth_user su ON su.id = s0.student_user_id
LEFT JOIN auth_user mu ON mu.id = s0.mentor_user_id
LEFT JOIN video_sessions_onetoonetoken tok ON tok.id = s0.one_to_one_token_id
LEFT JOIN LATERAL (
    SELECT lc.title AS module_batch
    FROM courses_courseusermapping lm
    JOIN courses_course lc ON lc.id = lm.course_id
    WHERE lm.user_id = s0.student_user_id AND lm.status = 8
      AND lc.course_structure_id IN (50,51,52,53,54,55,56,57,58,59,60,61,72,193,255,320,321)
      AND lc.start_timestamp <= s0.start_timestamp
    ORDER BY lc.start_timestamp DESC LIMIT 1
) mb ON TRUE
ORDER BY s0.start_timestamp DESC
