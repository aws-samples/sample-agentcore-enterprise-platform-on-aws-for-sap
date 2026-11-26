* Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
* SPDX-License-Identifier: Apache-2.0
*
* Sample code. Not for use in production. Review and adapt it to your system before use.

REPORT zaws_evt_drain.
* Schedule in SM36 every minute, as the system user ZCL_AWS_EVT_OUTBOX's header names.
* Sends what every use case recorded; drain( ) commits after each PutEvents call. The
* summary goes to the job log, never to a list: a job that writes a list needs a spool
* request, and on a system whose spool is full the job would abort after its sends.

START-OF-SELECTION.
  SPLIT zcl_aws_evt_outbox=>drain( ) AT |\n| INTO TABLE DATA(lt_lines).
  LOOP AT lt_lines INTO DATA(lv_line) WHERE table_line IS NOT INITIAL.
    MESSAGE lv_line TYPE 'S'.
  ENDLOOP.
