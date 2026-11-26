* Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
* SPDX-License-Identifier: Apache-2.0
*
* Sample code. Not for use in production. Review and adapt it to your system before use.

CLASS zcl_im_bo_ingest DEFINITION PUBLIC FINAL CREATE PUBLIC.
* finance_ops' BAdI implementation: it records one nudge per blocked supplier invoice in
* the SAP layer's generic outbox (layers/sap/abap/, ZCL_AWS_EVT_OUTBOX), whose drain job
* puts it on the layer's sap-events bus. Set the outbox up first, as its header says.
* Implements classic BAdI INVOICE_UPDATE (package MRM). Create the class, then in SE19
* create an implementation of INVOICE_UPDATE that names it; SXC_ATTR shows it active. On
* S/4HANA, SE19 also creates an enhancement implementation and asks for a ZEI_ name.
  PUBLIC SECTION.
    INTERFACES if_ex_invoice_update.
ENDCLASS.

CLASS zcl_im_bo_ingest IMPLEMENTATION.

  METHOD if_ex_invoice_update~change_at_save.
*   Empty: a new invoice has no BELNR yet, and the posting can still fail.
  ENDMETHOD.

  METHOD if_ex_invoice_update~change_before_update.
*   Empty, for the same reason.
  ENDMETHOD.

  METHOD if_ex_invoice_update~change_in_update.
*   The update task, after COMMIT WORK: BELNR is final. One INSERT, never a call to AWS.
*   The IF mirrors the finance_ops pack's `source.filter` (a payment block is set). A
*   looser IF only costs a nudge that makes nothing; a tighter one leaves the exception
*   to the poller. The topic is the use case's name, the body a nudge
*   (types/sap-nudge.schema.json) keyed by the pack's parent source.key fields.
    IF s_rbkp_new-zlspr IS NOT INITIAL.
      zcl_aws_evt_outbox=>record(
        iv_topic = 'business-operations'
        iv_body  = |\{"version":1,"pack_id":"finance_ops","keys":[|
                && |\{"SupplierInvoice":"{ s_rbkp_new-belnr }",|
                && |"FiscalYear":"{ s_rbkp_new-gjahr }"\}]\}| ).
    ENDIF.
  ENDMETHOD.

ENDCLASS.
