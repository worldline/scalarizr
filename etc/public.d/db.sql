BEGIN TRANSACTION;
DELETE FROM sqlite_sequence;
CREATE TABLE p2p_message (
    "id" INTEGER PRIMARY KEY,
    "message_id" TEXT,
    "response_id" TEXT,
    "message_name" TEXT,
    "message" TEXT,
    "queue" TEXT,
    "is_ingoing" INTEGER,
    "out_is_delivered" INTEGER,
    "out_delivery_attempts" INTEGER,
    "out_last_attempt_time" TEXT,
    "in_is_handled" INTEGER
);
CREATE TABLE log (
    "id" INTEGER PRIMARY KEY,
    "name" TEXT,
    "level" INTEGER,
    "pathname" TEXT,
    "lineno" INTEGER,
    "msg" TEXT,
    "stack_trace" TEXT
)
COMMIT;