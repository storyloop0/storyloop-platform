// Generated from api/contracts.py.
"use strict";
export const validate = validate20;
export default validate20;
const schema31 = {"$defs":{"BlockedTurnRecoveryStatus":{"additionalProperties":false,"properties":{"can_recover":{"const":false,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["INPUT_MISMATCH","LATER_REQUEST","EVIDENCE_INVALID","CONFIG_CHANGED","USAGE_UNAVAILABLE","LEGACY_REQUEST"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"blocked","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"BlockedTurnRecoveryStatus","type":"object"},"CompleteTurnRecoveryStatus":{"additionalProperties":false,"properties":{"can_recover":{"const":true,"title":"Can Recover","type":"boolean"},"reason_code":{"const":"RECEIPT_READY","title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"complete","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"CompleteTurnRecoveryStatus","type":"object"},"RecoverableTurnRecoveryStatus":{"additionalProperties":false,"properties":{"can_recover":{"const":true,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["PREPARED_READY","SCENE_READY"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"recoverable","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"RecoverableTurnRecoveryStatus","type":"object"},"UnavailableTurnRecoveryStatus":{"additionalProperties":false,"properties":{"can_recover":{"const":false,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["NO_SNAPSHOT","UNSUPPORTED"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"not_available","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"UnavailableTurnRecoveryStatus","type":"object"}},"discriminator":{"mapping":{"blocked":"#/$defs/BlockedTurnRecoveryStatus","complete":"#/$defs/CompleteTurnRecoveryStatus","not_available":"#/$defs/UnavailableTurnRecoveryStatus","recoverable":"#/$defs/RecoverableTurnRecoveryStatus"},"propertyName":"status"},"oneOf":[{"$ref":"#/$defs/CompleteTurnRecoveryStatus"},{"$ref":"#/$defs/RecoverableTurnRecoveryStatus"},{"$ref":"#/$defs/BlockedTurnRecoveryStatus"},{"$ref":"#/$defs/UnavailableTurnRecoveryStatus"}]};
const schema32 = {"additionalProperties":false,"properties":{"can_recover":{"const":true,"title":"Can Recover","type":"boolean"},"reason_code":{"const":"RECEIPT_READY","title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"complete","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"CompleteTurnRecoveryStatus","type":"object"};
const schema33 = {"additionalProperties":false,"properties":{"can_recover":{"const":true,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["PREPARED_READY","SCENE_READY"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"recoverable","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"RecoverableTurnRecoveryStatus","type":"object"};
const schema34 = {"additionalProperties":false,"properties":{"can_recover":{"const":false,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["INPUT_MISMATCH","LATER_REQUEST","EVIDENCE_INVALID","CONFIG_CHANGED","USAGE_UNAVAILABLE","LEGACY_REQUEST"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"blocked","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"BlockedTurnRecoveryStatus","type":"object"};
const schema35 = {"additionalProperties":false,"properties":{"can_recover":{"const":false,"title":"Can Recover","type":"boolean"},"reason_code":{"enum":["NO_SNAPSHOT","UNSUPPORTED"],"title":"Reason Code","type":"string"},"request_id":{"title":"Request Id","type":"string"},"status":{"const":"not_available","title":"Status","type":"string"}},"required":["request_id","status","can_recover","reason_code"],"title":"UnavailableTurnRecoveryStatus","type":"object"};

function validate20(data, {instancePath="", parentData, parentDataProperty, rootData=data, dynamicAnchors={}}={}){
let vErrors = null;
let errors = 0;
const evaluated0 = validate20.evaluated;
if(evaluated0.dynamicProps){
evaluated0.props = undefined;
}
if(evaluated0.dynamicItems){
evaluated0.items = undefined;
}
const _errs0 = errors;
let valid0 = false;
let passing0 = null;
const _errs1 = errors;
const _errs2 = errors;
if(errors === _errs2){
if(data && typeof data == "object" && !Array.isArray(data)){
let missing0;
if(((((data.request_id === undefined) && (missing0 = "request_id")) || ((data.status === undefined) && (missing0 = "status"))) || ((data.can_recover === undefined) && (missing0 = "can_recover"))) || ((data.reason_code === undefined) && (missing0 = "reason_code"))){
const err0 = {instancePath,schemaPath:"#/$defs/CompleteTurnRecoveryStatus/required",keyword:"required",params:{missingProperty: missing0},message:"must have required property '"+missing0+"'"};
if(vErrors === null){
vErrors = [err0];
}
else {
vErrors.push(err0);
}
errors++;
}
else {
const _errs4 = errors;
for(const key0 in data){
if(!((((key0 === "can_recover") || (key0 === "reason_code")) || (key0 === "request_id")) || (key0 === "status"))){
const err1 = {instancePath,schemaPath:"#/$defs/CompleteTurnRecoveryStatus/additionalProperties",keyword:"additionalProperties",params:{additionalProperty: key0},message:"must NOT have additional properties"};
if(vErrors === null){
vErrors = [err1];
}
else {
vErrors.push(err1);
}
errors++;
break;
}
}
if(_errs4 === errors){
if(data.can_recover !== undefined){
let data0 = data.can_recover;
const _errs5 = errors;
if(typeof data0 !== "boolean"){
const err2 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/can_recover/type",keyword:"type",params:{type: "boolean"},message:"must be boolean"};
if(vErrors === null){
vErrors = [err2];
}
else {
vErrors.push(err2);
}
errors++;
}
if(true !== data0){
const err3 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/can_recover/const",keyword:"const",params:{allowedValue: true},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err3];
}
else {
vErrors.push(err3);
}
errors++;
}
var valid2 = _errs5 === errors;
}
else {
var valid2 = true;
}
if(valid2){
if(data.reason_code !== undefined){
let data1 = data.reason_code;
const _errs7 = errors;
if(typeof data1 !== "string"){
const err4 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/reason_code/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err4];
}
else {
vErrors.push(err4);
}
errors++;
}
if("RECEIPT_READY" !== data1){
const err5 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/reason_code/const",keyword:"const",params:{allowedValue: "RECEIPT_READY"},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err5];
}
else {
vErrors.push(err5);
}
errors++;
}
var valid2 = _errs7 === errors;
}
else {
var valid2 = true;
}
if(valid2){
if(data.request_id !== undefined){
const _errs9 = errors;
if(typeof data.request_id !== "string"){
const err6 = {instancePath:instancePath+"/request_id",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/request_id/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err6];
}
else {
vErrors.push(err6);
}
errors++;
}
var valid2 = _errs9 === errors;
}
else {
var valid2 = true;
}
if(valid2){
if(data.status !== undefined){
let data3 = data.status;
const _errs11 = errors;
if(typeof data3 !== "string"){
const err7 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/status/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err7];
}
else {
vErrors.push(err7);
}
errors++;
}
if("complete" !== data3){
const err8 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/CompleteTurnRecoveryStatus/properties/status/const",keyword:"const",params:{allowedValue: "complete"},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err8];
}
else {
vErrors.push(err8);
}
errors++;
}
var valid2 = _errs11 === errors;
}
else {
var valid2 = true;
}
}
}
}
}
}
}
else {
const err9 = {instancePath,schemaPath:"#/$defs/CompleteTurnRecoveryStatus/type",keyword:"type",params:{type: "object"},message:"must be object"};
if(vErrors === null){
vErrors = [err9];
}
else {
vErrors.push(err9);
}
errors++;
}
}
var _valid0 = _errs1 === errors;
if(_valid0){
valid0 = true;
passing0 = 0;
var props0 = true;
}
const _errs13 = errors;
const _errs14 = errors;
if(errors === _errs14){
if(data && typeof data == "object" && !Array.isArray(data)){
let missing1;
if(((((data.request_id === undefined) && (missing1 = "request_id")) || ((data.status === undefined) && (missing1 = "status"))) || ((data.can_recover === undefined) && (missing1 = "can_recover"))) || ((data.reason_code === undefined) && (missing1 = "reason_code"))){
const err10 = {instancePath,schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/required",keyword:"required",params:{missingProperty: missing1},message:"must have required property '"+missing1+"'"};
if(vErrors === null){
vErrors = [err10];
}
else {
vErrors.push(err10);
}
errors++;
}
else {
const _errs16 = errors;
for(const key1 in data){
if(!((((key1 === "can_recover") || (key1 === "reason_code")) || (key1 === "request_id")) || (key1 === "status"))){
const err11 = {instancePath,schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/additionalProperties",keyword:"additionalProperties",params:{additionalProperty: key1},message:"must NOT have additional properties"};
if(vErrors === null){
vErrors = [err11];
}
else {
vErrors.push(err11);
}
errors++;
break;
}
}
if(_errs16 === errors){
if(data.can_recover !== undefined){
let data4 = data.can_recover;
const _errs17 = errors;
if(typeof data4 !== "boolean"){
const err12 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/can_recover/type",keyword:"type",params:{type: "boolean"},message:"must be boolean"};
if(vErrors === null){
vErrors = [err12];
}
else {
vErrors.push(err12);
}
errors++;
}
if(true !== data4){
const err13 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/can_recover/const",keyword:"const",params:{allowedValue: true},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err13];
}
else {
vErrors.push(err13);
}
errors++;
}
var valid4 = _errs17 === errors;
}
else {
var valid4 = true;
}
if(valid4){
if(data.reason_code !== undefined){
let data5 = data.reason_code;
const _errs19 = errors;
if(typeof data5 !== "string"){
const err14 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/reason_code/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err14];
}
else {
vErrors.push(err14);
}
errors++;
}
if(!((data5 === "PREPARED_READY") || (data5 === "SCENE_READY"))){
const err15 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/reason_code/enum",keyword:"enum",params:{allowedValues: schema33.properties.reason_code.enum},message:"must be equal to one of the allowed values"};
if(vErrors === null){
vErrors = [err15];
}
else {
vErrors.push(err15);
}
errors++;
}
var valid4 = _errs19 === errors;
}
else {
var valid4 = true;
}
if(valid4){
if(data.request_id !== undefined){
const _errs21 = errors;
if(typeof data.request_id !== "string"){
const err16 = {instancePath:instancePath+"/request_id",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/request_id/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err16];
}
else {
vErrors.push(err16);
}
errors++;
}
var valid4 = _errs21 === errors;
}
else {
var valid4 = true;
}
if(valid4){
if(data.status !== undefined){
let data7 = data.status;
const _errs23 = errors;
if(typeof data7 !== "string"){
const err17 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/status/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err17];
}
else {
vErrors.push(err17);
}
errors++;
}
if("recoverable" !== data7){
const err18 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/properties/status/const",keyword:"const",params:{allowedValue: "recoverable"},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err18];
}
else {
vErrors.push(err18);
}
errors++;
}
var valid4 = _errs23 === errors;
}
else {
var valid4 = true;
}
}
}
}
}
}
}
else {
const err19 = {instancePath,schemaPath:"#/$defs/RecoverableTurnRecoveryStatus/type",keyword:"type",params:{type: "object"},message:"must be object"};
if(vErrors === null){
vErrors = [err19];
}
else {
vErrors.push(err19);
}
errors++;
}
}
var _valid0 = _errs13 === errors;
if(_valid0 && valid0){
valid0 = false;
passing0 = [passing0, 1];
}
else {
if(_valid0){
valid0 = true;
passing0 = 1;
if(props0 !== true){
props0 = true;
}
}
const _errs25 = errors;
const _errs26 = errors;
if(errors === _errs26){
if(data && typeof data == "object" && !Array.isArray(data)){
let missing2;
if(((((data.request_id === undefined) && (missing2 = "request_id")) || ((data.status === undefined) && (missing2 = "status"))) || ((data.can_recover === undefined) && (missing2 = "can_recover"))) || ((data.reason_code === undefined) && (missing2 = "reason_code"))){
const err20 = {instancePath,schemaPath:"#/$defs/BlockedTurnRecoveryStatus/required",keyword:"required",params:{missingProperty: missing2},message:"must have required property '"+missing2+"'"};
if(vErrors === null){
vErrors = [err20];
}
else {
vErrors.push(err20);
}
errors++;
}
else {
const _errs28 = errors;
for(const key2 in data){
if(!((((key2 === "can_recover") || (key2 === "reason_code")) || (key2 === "request_id")) || (key2 === "status"))){
const err21 = {instancePath,schemaPath:"#/$defs/BlockedTurnRecoveryStatus/additionalProperties",keyword:"additionalProperties",params:{additionalProperty: key2},message:"must NOT have additional properties"};
if(vErrors === null){
vErrors = [err21];
}
else {
vErrors.push(err21);
}
errors++;
break;
}
}
if(_errs28 === errors){
if(data.can_recover !== undefined){
let data8 = data.can_recover;
const _errs29 = errors;
if(typeof data8 !== "boolean"){
const err22 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/can_recover/type",keyword:"type",params:{type: "boolean"},message:"must be boolean"};
if(vErrors === null){
vErrors = [err22];
}
else {
vErrors.push(err22);
}
errors++;
}
if(false !== data8){
const err23 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/can_recover/const",keyword:"const",params:{allowedValue: false},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err23];
}
else {
vErrors.push(err23);
}
errors++;
}
var valid6 = _errs29 === errors;
}
else {
var valid6 = true;
}
if(valid6){
if(data.reason_code !== undefined){
let data9 = data.reason_code;
const _errs31 = errors;
if(typeof data9 !== "string"){
const err24 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/reason_code/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err24];
}
else {
vErrors.push(err24);
}
errors++;
}
if(!((((((data9 === "INPUT_MISMATCH") || (data9 === "LATER_REQUEST")) || (data9 === "EVIDENCE_INVALID")) || (data9 === "CONFIG_CHANGED")) || (data9 === "USAGE_UNAVAILABLE")) || (data9 === "LEGACY_REQUEST"))){
const err25 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/reason_code/enum",keyword:"enum",params:{allowedValues: schema34.properties.reason_code.enum},message:"must be equal to one of the allowed values"};
if(vErrors === null){
vErrors = [err25];
}
else {
vErrors.push(err25);
}
errors++;
}
var valid6 = _errs31 === errors;
}
else {
var valid6 = true;
}
if(valid6){
if(data.request_id !== undefined){
const _errs33 = errors;
if(typeof data.request_id !== "string"){
const err26 = {instancePath:instancePath+"/request_id",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/request_id/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err26];
}
else {
vErrors.push(err26);
}
errors++;
}
var valid6 = _errs33 === errors;
}
else {
var valid6 = true;
}
if(valid6){
if(data.status !== undefined){
let data11 = data.status;
const _errs35 = errors;
if(typeof data11 !== "string"){
const err27 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/status/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err27];
}
else {
vErrors.push(err27);
}
errors++;
}
if("blocked" !== data11){
const err28 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/BlockedTurnRecoveryStatus/properties/status/const",keyword:"const",params:{allowedValue: "blocked"},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err28];
}
else {
vErrors.push(err28);
}
errors++;
}
var valid6 = _errs35 === errors;
}
else {
var valid6 = true;
}
}
}
}
}
}
}
else {
const err29 = {instancePath,schemaPath:"#/$defs/BlockedTurnRecoveryStatus/type",keyword:"type",params:{type: "object"},message:"must be object"};
if(vErrors === null){
vErrors = [err29];
}
else {
vErrors.push(err29);
}
errors++;
}
}
var _valid0 = _errs25 === errors;
if(_valid0 && valid0){
valid0 = false;
passing0 = [passing0, 2];
}
else {
if(_valid0){
valid0 = true;
passing0 = 2;
if(props0 !== true){
props0 = true;
}
}
const _errs37 = errors;
const _errs38 = errors;
if(errors === _errs38){
if(data && typeof data == "object" && !Array.isArray(data)){
let missing3;
if(((((data.request_id === undefined) && (missing3 = "request_id")) || ((data.status === undefined) && (missing3 = "status"))) || ((data.can_recover === undefined) && (missing3 = "can_recover"))) || ((data.reason_code === undefined) && (missing3 = "reason_code"))){
const err30 = {instancePath,schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/required",keyword:"required",params:{missingProperty: missing3},message:"must have required property '"+missing3+"'"};
if(vErrors === null){
vErrors = [err30];
}
else {
vErrors.push(err30);
}
errors++;
}
else {
const _errs40 = errors;
for(const key3 in data){
if(!((((key3 === "can_recover") || (key3 === "reason_code")) || (key3 === "request_id")) || (key3 === "status"))){
const err31 = {instancePath,schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/additionalProperties",keyword:"additionalProperties",params:{additionalProperty: key3},message:"must NOT have additional properties"};
if(vErrors === null){
vErrors = [err31];
}
else {
vErrors.push(err31);
}
errors++;
break;
}
}
if(_errs40 === errors){
if(data.can_recover !== undefined){
let data12 = data.can_recover;
const _errs41 = errors;
if(typeof data12 !== "boolean"){
const err32 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/can_recover/type",keyword:"type",params:{type: "boolean"},message:"must be boolean"};
if(vErrors === null){
vErrors = [err32];
}
else {
vErrors.push(err32);
}
errors++;
}
if(false !== data12){
const err33 = {instancePath:instancePath+"/can_recover",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/can_recover/const",keyword:"const",params:{allowedValue: false},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err33];
}
else {
vErrors.push(err33);
}
errors++;
}
var valid8 = _errs41 === errors;
}
else {
var valid8 = true;
}
if(valid8){
if(data.reason_code !== undefined){
let data13 = data.reason_code;
const _errs43 = errors;
if(typeof data13 !== "string"){
const err34 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/reason_code/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err34];
}
else {
vErrors.push(err34);
}
errors++;
}
if(!((data13 === "NO_SNAPSHOT") || (data13 === "UNSUPPORTED"))){
const err35 = {instancePath:instancePath+"/reason_code",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/reason_code/enum",keyword:"enum",params:{allowedValues: schema35.properties.reason_code.enum},message:"must be equal to one of the allowed values"};
if(vErrors === null){
vErrors = [err35];
}
else {
vErrors.push(err35);
}
errors++;
}
var valid8 = _errs43 === errors;
}
else {
var valid8 = true;
}
if(valid8){
if(data.request_id !== undefined){
const _errs45 = errors;
if(typeof data.request_id !== "string"){
const err36 = {instancePath:instancePath+"/request_id",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/request_id/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err36];
}
else {
vErrors.push(err36);
}
errors++;
}
var valid8 = _errs45 === errors;
}
else {
var valid8 = true;
}
if(valid8){
if(data.status !== undefined){
let data15 = data.status;
const _errs47 = errors;
if(typeof data15 !== "string"){
const err37 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/status/type",keyword:"type",params:{type: "string"},message:"must be string"};
if(vErrors === null){
vErrors = [err37];
}
else {
vErrors.push(err37);
}
errors++;
}
if("not_available" !== data15){
const err38 = {instancePath:instancePath+"/status",schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/properties/status/const",keyword:"const",params:{allowedValue: "not_available"},message:"must be equal to constant"};
if(vErrors === null){
vErrors = [err38];
}
else {
vErrors.push(err38);
}
errors++;
}
var valid8 = _errs47 === errors;
}
else {
var valid8 = true;
}
}
}
}
}
}
}
else {
const err39 = {instancePath,schemaPath:"#/$defs/UnavailableTurnRecoveryStatus/type",keyword:"type",params:{type: "object"},message:"must be object"};
if(vErrors === null){
vErrors = [err39];
}
else {
vErrors.push(err39);
}
errors++;
}
}
var _valid0 = _errs37 === errors;
if(_valid0 && valid0){
valid0 = false;
passing0 = [passing0, 3];
}
else {
if(_valid0){
valid0 = true;
passing0 = 3;
if(props0 !== true){
props0 = true;
}
}
}
}
}
if(!valid0){
const err40 = {instancePath,schemaPath:"#/oneOf",keyword:"oneOf",params:{passingSchemas: passing0},message:"must match exactly one schema in oneOf"};
if(vErrors === null){
vErrors = [err40];
}
else {
vErrors.push(err40);
}
errors++;
validate20.errors = vErrors;
return false;
}
else {
errors = _errs0;
if(vErrors !== null){
if(_errs0){
vErrors.length = _errs0;
}
else {
vErrors = null;
}
}
}
validate20.errors = vErrors;
evaluated0.props = props0;
return errors === 0;
}
validate20.evaluated = {"dynamicProps":true,"dynamicItems":false};
