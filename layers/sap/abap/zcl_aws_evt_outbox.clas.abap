* Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
* SPDX-License-Identifier: Apache-2.0
*
* Sample code. Not for use in production. Review and adapt it to your system before use.

CLASS zcl_aws_evt_outbox DEFINITION PUBLIC FINAL CREATE PUBLIC.
* The SAP layer's generic outbox (layers/sap/reference/events.md). A use case's
* BAdI implementation calls record( topic, body ) in the update task; the drain job puts
* each row on the layer's sap-events bus with the AWS SDK for SAP ABAP, as an event with
* source sap.<this system>, detail-type = the topic (the use case's name) and detail = the
* body. The layer's sender role for this system may put only its own source and the
* topics of the use cases whose layers.sap.access.<use case>.ingest names it; the bus
* refuses every other principal. The body is the use case's contract, never read here:
* for business-operations, a nudge (types/sap-nudge.schema.json), which carries keys,
* never document data. Whatever this outbox loses, the use case's poller still finds.
*
* Setup, once per SAP system. The sample ships as source, not a transport: create table
* ZAWS_EVT_OUTBOX, this class and report ZAWS_EVT_DRAIN on each system, as local $TMP
* objects to try it, or in a customer package with a transport request to move them on.
* Each use case then adds only its own BAdI implementation calling record( ).
* - The SDK's core and EventBridge (EVB) transports; check TADIR for package /AWS1/API_EVB.
*   In /AWS1/IMG, on a system that has never used the SDK: the global configuration (EC2
*   metadata endpoint mode IPv4 on EC2), the runtime settings (active scenario DEFAULT,
*   trace level 1), and scenario DEFAULT itself. Then profile ZAWS_EVT for this SID and
*   client: the bus's Region, this system's authentication method (INST, the instance
*   role, on EC2; IAM Roles Anywhere or SSF-stored keys on premises), and an IAM role
*   mapping from logical role SENDER to this system's sender role. Read its ARN from SSM
*   `/<project>/<env>/layers/sap/systems/<system>/sender-role-arn`, never from the role
*   name, which CloudFormation truncates. The role trusts exactly the principals in
*   layers.sap.systems.<system>.senders, and each of them needs sts:AssumeRole on it.
* - TVARVC parameters ZAWS_EVT_BUS_ARN, the bus ARN from SSM
*   `/<project>/<env>/layers/sap/bus-arn`, and ZAWS_EVT_SYSTEM, this system's name in
*   layers.sap.systems. Neither is in source, so one transport serves every system.
* - Report ZAWS_EVT_DRAIN in SM36 every minute, as a system user (type B), never a
*   person's dialog user. The job needs one role with three objects: /AWS1/SESS
*   (/AWS1/PROF = ZAWS_EVT) and /AWS1/LROL (/AWS1/PROF = ZAWS_EVT, /AWS1/LROL = SENDER)
*   for the SDK, and S_PROGNAM (P_ACTION = BTCSUBMIT, P_PROGNAM = ZAWS_EVT_DRAIN) to be
*   the job's step user. For a report with no authorization group SAP checks S_PROGNAM,
*   not S_PROGRAM, and SM36 answers BT 107 ("not authorized to schedule program ... as a
*   background job") until the role has it.
* - Dry run: classrun this class over ADT; it prints each event's source, detail-type and
*   detail, and sends and changes nothing.
*
* The SDK class and method names (/AWS1/CL_EVB_FACTORY, /AWS1/CL_EVBPUTEVENTSREQENTRY,
* PUTEVENTS, GET_ENTRIES, GET_ERRORCODE) are from the SDK's API reference and are to be
* confirmed on activation.
*
* A failed entry marks its row E and the next drain retries it, at most c_attempts times;
* after that a row stays E and only the poller still makes its case. Once the cause is
* fixed (a topic no ceiling names, a missing sts:AssumeRole), replay a row by setting its
* attempts back to 0: the next drain sends it again. Sent rows are deleted after
* c_keep_days.
  PUBLIC SECTION.
    INTERFACES if_oo_adt_classrun.

*   The update task's entry point: one INSERT. Never raises, never does I/O.
    CLASS-METHODS record
      IMPORTING iv_topic TYPE clike
                iv_body  TYPE string.

*   Sends every pending row, one PutEvents call per at most c_batch rows of one topic,
*   and commits after each call. A dry run sends, changes and commits nothing.
    CLASS-METHODS drain
      IMPORTING iv_dry_run    TYPE abap_bool DEFAULT abap_false
      RETURNING VALUE(rv_log) TYPE string.

  PRIVATE SECTION.
    TYPES: BEGIN OF ty_row,
             guid  TYPE sysuuid_x16,
             topic TYPE c LENGTH 64,
             body  TYPE string,
           END OF ty_row,
           ty_rows TYPE STANDARD TABLE OF ty_row WITH EMPTY KEY.
    CONSTANTS c_profile   TYPE /aws1/rt_profile_id VALUE 'ZAWS_EVT'.
*   PutEvents takes at most 10 entries and 1 MB per call. A body over c_alone goes in a
*   call of its own; one over c_max (1 MB less the entry's envelope) is never sent.
    CONSTANTS c_batch     TYPE i VALUE 10.
    CONSTANTS c_alone     TYPE i VALUE 102400.
    CONSTANTS c_max       TYPE i VALUE 1040000.
    CONSTANTS c_attempts  TYPE i VALUE 5.
    CONSTANTS c_keep_days TYPE i VALUE 7.
    CLASS-DATA go_evb TYPE REF TO /aws1/if_evb.
    CLASS-DATA gv_bus TYPE string.
    CLASS-DATA gv_system TYPE string.

*   One PutEvents call; marks each row by its own entry's result. Commits.
    CLASS-METHODS send
      IMPORTING it_rows       TYPE ty_rows
                iv_dry_run    TYPE abap_bool
      RETURNING VALUE(rv_log) TYPE string.

    CLASS-METHODS fail
      IMPORTING it_rows TYPE ty_rows.
ENDCLASS.

CLASS zcl_aws_evt_outbox IMPLEMENTATION.

  METHOD if_oo_adt_classrun~main.
    out->write( drain( iv_dry_run = abap_true ) ).
  ENDMETHOD.

  METHOD record.
    TRY.
        GET TIME STAMP FIELD DATA(lv_now).
        INSERT zaws_evt_outbox FROM @( VALUE #(
          guid     = cl_system_uuid=>create_uuid_x16_static( )
          topic    = iv_topic
          body     = iv_body
          status   = 'N'
          attempts = 0
          created  = lv_now ) ).
      CATCH cx_root.
*       The posting must complete even if the outbox does not; the poller covers it.
    ENDTRY.
  ENDMETHOD.

  METHOD drain.
    IF gv_bus IS INITIAL.
      SELECT SINGLE low FROM tvarvc
        WHERE name = 'ZAWS_EVT_BUS_ARN' AND type = 'P' INTO @gv_bus.
      SELECT SINGLE low FROM tvarvc
        WHERE name = 'ZAWS_EVT_SYSTEM' AND type = 'P' INTO @gv_system.
    ENDIF.
    IF gv_bus IS INITIAL OR gv_system IS INITIAL.
      rv_log = |TVARVC ZAWS_EVT_BUS_ARN or ZAWS_EVT_SYSTEM is not set|.
      RETURN.
    ENDIF.

    SELECT guid, topic, body FROM zaws_evt_outbox
      WHERE status IN ( 'N', 'E' ) AND attempts < @c_attempts
      ORDER BY topic, created
      INTO TABLE @DATA(lt_pending) UP TO 500 ROWS.

    IF lt_pending IS NOT INITIAL AND iv_dry_run = abap_false AND go_evb IS INITIAL.
      TRY.
          go_evb = /aws1/cl_evb_factory=>create(
            /aws1/cl_rt_session_aws=>create( iv_profile_id = c_profile ) ).
        CATCH cx_root INTO DATA(lx).
          rv_log = |no SDK session: { lx->get_text( ) }|.
          RETURN.
      ENDTRY.
    ENDIF.

*   One topic per call: the sender role refuses a whole call when one entry names a
*   topic no ceiling allows, so a bad topic fails only its own rows.
    DATA lt_batch TYPE ty_rows.
    LOOP AT lt_pending INTO DATA(ls_row) GROUP BY ls_row-topic INTO DATA(lv_topic).
      LOOP AT GROUP lv_topic INTO DATA(ls_member).
        DATA(lv_size) = strlen( ls_member-body ).
        IF lv_size > c_max.
          IF iv_dry_run = abap_false.
            UPDATE zaws_evt_outbox SET status = 'E', attempts = @c_attempts
              WHERE guid = @ls_member-guid.
            COMMIT WORK.
          ENDIF.
          rv_log = rv_log && |{ lv_topic }: a body of { lv_size } bytes is never sent\n|.
          CONTINUE.
        ENDIF.
        IF lv_size > c_alone AND lt_batch IS NOT INITIAL.
          rv_log = rv_log && send( it_rows = lt_batch iv_dry_run = iv_dry_run ).
          CLEAR lt_batch.
        ENDIF.
        APPEND CORRESPONDING #( ls_member ) TO lt_batch.
        IF lines( lt_batch ) = c_batch OR lv_size > c_alone.
          rv_log = rv_log && send( it_rows = lt_batch iv_dry_run = iv_dry_run ).
          CLEAR lt_batch.
        ENDIF.
      ENDLOOP.
      IF lt_batch IS NOT INITIAL.
        rv_log = rv_log && send( it_rows = lt_batch iv_dry_run = iv_dry_run ).
        CLEAR lt_batch.
      ENDIF.
    ENDLOOP.
    IF rv_log IS INITIAL.
      rv_log = |nothing pending\n|.
    ENDIF.

    IF iv_dry_run = abap_false.
      DATA lv_cut TYPE timestampl.
      DATA(lv_day) = CONV d( sy-datum - c_keep_days ).
      CONVERT DATE lv_day TIME '000000' INTO TIME STAMP lv_cut TIME ZONE 'UTC'.
      DELETE FROM zaws_evt_outbox WHERE status = 'S' AND created < @lv_cut.
      COMMIT WORK.
    ENDIF.
  ENDMETHOD.

  METHOD send.
    IF iv_dry_run = abap_true.
      LOOP AT it_rows INTO DATA(ls_dry).
        rv_log = rv_log && |sap.{ gv_system } { ls_dry-topic } { ls_dry-body }\n|.
      ENDLOOP.
      RETURN.
    ENDIF.

    DATA lt_entries TYPE /aws1/cl_evbputeventsreqentry=>tt_puteventsrequestentrylist.
    LOOP AT it_rows INTO DATA(ls_row).
      APPEND NEW /aws1/cl_evbputeventsreqentry(
        iv_eventbusname = gv_bus
        iv_source       = |sap.{ gv_system }|
        iv_detailtype   = CONV #( ls_row-topic )
        iv_detail       = ls_row-body ) TO lt_entries.
    ENDLOOP.

    TRY.
        DATA(lt_result) = go_evb->putevents( it_entries = lt_entries )->get_entries( ).
        DATA(lv_sent) = 0.
*       The response's entries are index-aligned with the request's.
        LOOP AT it_rows INTO ls_row.
          DATA(lv_index) = sy-tabix.
          IF lt_result[ lv_index ]->get_errorcode( ) IS INITIAL.
            UPDATE zaws_evt_outbox SET status = 'S' WHERE guid = @ls_row-guid.
            lv_sent = lv_sent + 1.
          ELSE.
            UPDATE zaws_evt_outbox SET status = 'E', attempts = attempts + 1
              WHERE guid = @ls_row-guid.
            rv_log = rv_log && |{ ls_row-topic }: { lt_result[ lv_index ]->get_errorcode( ) }\n|.
          ENDIF.
        ENDLOOP.
        rv_log = rv_log && |{ ls_row-topic }: sent { lv_sent } of { lines( it_rows ) }\n|.
      CATCH cx_root INTO DATA(lx).
*       The whole call failed (AccessDenied, network, throttling): every row retries.
        fail( it_rows ).
        rv_log = rv_log && |failed { lines( it_rows ) } events: { lx->get_text( ) }\n|.
    ENDTRY.
*   At most one call's rows are resent if the job dies after this; a resend is harmless.
    COMMIT WORK.
  ENDMETHOD.

  METHOD fail.
    LOOP AT it_rows INTO DATA(ls_row).
      UPDATE zaws_evt_outbox SET status = 'E', attempts = attempts + 1
        WHERE guid = @ls_row-guid.
    ENDLOOP.
  ENDMETHOD.

ENDCLASS.
