// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Sample code. Not for use in production. Review and adapt it to your system before use.
// One row per event a use case recorded for the SAP layer's sap-events bus. topic is the
// use case's name (the event's detail-type, at most 41 characters); body is its detail, a
// JSON object the drain never parses. status is N new, E failed (retried), S sent.
// The name is 15 characters, inside SAP's 16 for a table.


@EndUserText.label : 'AWS SAP layer: event outbox'
@AbapCatalog.enhancement.category : #NOT_EXTENSIBLE
@AbapCatalog.tableCategory : #TRANSPARENT
@AbapCatalog.deliveryClass : #A
@AbapCatalog.dataMaintenance : #RESTRICTED
define table zaws_evt_outbox {

  key mandt : mandt not null;
  key guid  : sysuuid_x16 not null;
  topic     : abap.char(64) not null;
  body      : abap.string(0);
  status    : abap.char(1) not null;
  attempts  : abap.int1 not null;
  created   : timestampl not null;

}
