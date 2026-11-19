# API behavior

The current original HTTP routes and request parameters are retained. Core data readers execute their original database queries with private calculation fallbacks replaced by stored-output projections. Database connection/schema failure returns 503. Localization and source-strategy metadata are implemented.

Only private calculation configuration and rule endpoints return 501: GET `/stage5/rules`, POST `/stage5/rules/validate`, PUT `/stage5/rules`, POST `/stage5/rules/reset` and GET `/stage5/rules/history`. Carbon status/report/export readers access stored outputs without loading private coefficient configuration.

The OpenAPI snapshot is generated from the restored application. It is not evidence of a populated database or working private producer. No fake empty dataset or calculated example is supplied.
