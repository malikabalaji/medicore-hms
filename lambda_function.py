"""
MediCore Hospital Management System: backend Lambda
Single DynamoDB table (HospitalTable) with one Global Secondary Index (GSI1).

Item design
  Patient     PK=PATIENT#<id>  SK=PROFILE                 GSI1PK=TYPE#PATIENT  GSI1SK=<name>
  Doctor      PK=DOCTOR#<id>   SK=PROFILE                 GSI1PK=TYPE#DOCTOR   GSI1SK=<dept>#<name>
  Appointment PK=PATIENT#<id>  SK=APPT#<date>T<time>#<id> GSI1PK=DOCTOR#<id>   GSI1SK=APPT#<date>T<time>
  Slot lock   PK=DOCTOR#<id>   SK=SLOT#<date>T<time>      (prevents double booking)
  Counter     PK=COUNTER       SK=PATIENT | DOCTOR | APPOINTMENT  (atomic ID sequence)

DynamoDB features used
  Query on table and GSI, GetItem, PutItem with condition expressions,
  UpdateItem with atomic ADD counters, TransactWriteItems (all-or-nothing writes).

Routes (API Gateway HTTP API, payload format 2.0)
  GET    /patients
  POST   /patients
  GET    /patients/{id}/appointments
  GET    /doctors
  POST   /doctors
  GET    /doctors/{id}/appointments
  POST   /appointments
  PATCH  /appointments
  GET    /stats
  POST   /seed
"""

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME") or os.environ.get("table_name") or "HospitalTable"
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
client = dynamodb.meta.client

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
}


class Conflict(Exception):
    pass


def _default(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    raise TypeError


def respond(status, body):
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body, default=_default)}


def today_ist():
    return datetime.now(IST).date().isoformat()


def query_all(**kwargs):
    items, resp = [], table.query(**kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.query(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    return items


# ---------- Atomic counter IDs (P001, D001, A0001) ----------
def next_number(kind):
    resp = table.update_item(
        Key={"PK": "COUNTER", "SK": kind},
        UpdateExpression="ADD n :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["n"])


def put_new(item_for_id, kind, prefix, width):
    """Create an item with the next free sequential ID. The condition stops it overwriting an existing record."""
    for _ in range(100):
        new_id = f"{prefix}{next_number(kind):0{width}d}"
        item = item_for_id(new_id)
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
            return item
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError("Could not allocate an ID")


# ---------- Patients ----------
def list_patients():
    return query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#PATIENT"))


def patient_item(pid, data):
    return {
        "PK": f"PATIENT#{pid}", "SK": "PROFILE",
        "GSI1PK": "TYPE#PATIENT", "GSI1SK": data["name"],
        "entity": "Patient", "patientId": pid,
        "name": data["name"], "age": int(data["age"]), "gender": data["gender"],
        "phone": data["phone"], "bloodGroup": data.get("bloodGroup", ""),
        "createdAt": datetime.now(IST).isoformat(timespec="seconds"),
    }


def create_patient(data):
    for f in ("name", "age", "gender", "phone"):
        if not str(data.get(f, "")).strip():
            raise ValueError(f"Missing field: {f}")
    if not re.fullmatch(r"\d{10}", re.sub(r"[\s-]", "", str(data["phone"]))):
        raise ValueError("Phone must be a 10-digit number")
    if not 0 <= int(data["age"]) <= 120:
        raise ValueError("Age must be between 0 and 120")
    if data.get("patientId"):
        item = patient_item(data["patientId"], data)
        table.put_item(Item=item)
        return item
    return put_new(lambda pid: patient_item(pid, data), "PATIENT", "P", 3)


def patient_appointments(pid):
    return query_all(KeyConditionExpression=Key("PK").eq(f"PATIENT#{pid}") & Key("SK").begins_with("APPT#"))


# ---------- Doctors ----------
def list_doctors():
    return query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("TYPE#DOCTOR"))


def doctor_item(did, data):
    return {
        "PK": f"DOCTOR#{did}", "SK": "PROFILE",
        "GSI1PK": "TYPE#DOCTOR", "GSI1SK": f"{data['department']}#{data['name']}",
        "entity": "Doctor", "doctorId": did,
        "name": data["name"], "department": data["department"],
        "phone": data.get("phone", ""),
    }


def create_doctor(data):
    for f in ("name", "department"):
        if not str(data.get(f, "")).strip():
            raise ValueError(f"Missing field: {f}")
    if data.get("doctorId"):
        item = doctor_item(data["doctorId"], data)
        table.put_item(Item=item)
        return item
    return put_new(lambda did: doctor_item(did, data), "DOCTOR", "D", 3)


def doctor_appointments(did):
    return query_all(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"DOCTOR#{did}") & Key("GSI1SK").begins_with("APPT#"))


# ---------- Appointments ----------
def slot_key(doctor_id, slot):
    return {"PK": f"DOCTOR#{doctor_id}", "SK": f"SLOT#{slot}"}


def create_appointment(data, allow_past=False):
    for f in ("patientId", "doctorId", "date", "time"):
        if not data.get(f):
            raise ValueError(f"Missing field: {f}")
    if not allow_past and data["date"] < today_ist():
        raise ValueError("Appointments cannot be booked in the past")
    patient = table.get_item(Key={"PK": f"PATIENT#{data['patientId']}", "SK": "PROFILE"}).get("Item")
    doctor = table.get_item(Key={"PK": f"DOCTOR#{data['doctorId']}", "SK": "PROFILE"}).get("Item")
    if not patient or not doctor:
        raise ValueError("Patient or doctor not found")

    aid = f"A{next_number('APPOINTMENT'):04d}"
    slot = f"{data['date']}T{data['time']}"
    item = {
        "PK": f"PATIENT#{data['patientId']}", "SK": f"APPT#{slot}#{aid}",
        "GSI1PK": f"DOCTOR#{data['doctorId']}", "GSI1SK": f"APPT#{slot}",
        "entity": "Appointment", "appointmentId": aid,
        "patientId": data["patientId"], "patientName": patient["name"],
        "doctorId": data["doctorId"], "doctorName": doctor["name"], "department": doctor["department"],
        "date": data["date"], "time": data["time"],
        "reason": data.get("reason", ""), "status": "Scheduled",
        "bookedAt": datetime.now(IST).isoformat(timespec="seconds"),
    }
    lock = {**slot_key(data["doctorId"], slot), "entity": "SlotLock", "appointmentId": aid}

    # One transaction: the slot lock and the appointment are written together or not at all.
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {"TableName": TABLE_NAME, "Item": lock, "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": TABLE_NAME, "Item": item}},
        ])
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise Conflict(f"{doctor['name']} is already booked on {data['date']} at {data['time']}. Choose another slot.")
        raise
    return item


def update_appointment(data):
    if not data.get("PK") or not data.get("SK") or not data.get("status"):
        raise ValueError("PK, SK and status are required")
    new = data["status"]
    if new not in ("Scheduled", "Completed", "Cancelled"):
        raise ValueError("Invalid status")
    current = table.get_item(Key={"PK": data["PK"], "SK": data["SK"]}).get("Item")
    if not current or current.get("entity") != "Appointment":
        raise ValueError("Appointment not found")
    old = current["status"]
    lock = slot_key(current["doctorId"], f"{current['date']}T{current['time']}")

    ops = [{"Update": {
        "TableName": TABLE_NAME,
        "Key": {"PK": data["PK"], "SK": data["SK"]},
        "UpdateExpression": "SET #s = :s, updatedAt = :t",
        "ConditionExpression": "attribute_exists(PK)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":s": new, ":t": datetime.now(IST).isoformat(timespec="seconds")},
    }}]
    if new == "Cancelled" and old != "Cancelled":
        ops.append({"Delete": {"TableName": TABLE_NAME, "Key": lock}})  # free the slot
    if old == "Cancelled" and new != "Cancelled":
        ops.append({"Put": {"TableName": TABLE_NAME,
                            "Item": {**lock, "entity": "SlotLock", "appointmentId": current["appointmentId"]},
                            "ConditionExpression": "attribute_not_exists(PK)"}})
    try:
        client.transact_write_items(TransactItems=ops)
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            raise Conflict("That slot has been taken by another booking, so this appointment cannot be restored.")
        raise
    return {**current, "status": new}


# ---------- Dashboard analytics ----------
def stats():
    patients = list_patients()
    doctors = list_doctors()
    appts = []
    for d in doctors:
        appts.extend(doctor_appointments(d["doctorId"]))
    today = today_ist()
    start = datetime.now(IST).date()
    next7 = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    active = [a for a in appts if a["status"] != "Cancelled"]
    per_day = Counter(a["date"] for a in active)
    todays = sorted((a for a in appts if a["date"] == today), key=lambda a: a["time"])
    return {
        "patients": len(patients),
        "doctors": len(doctors),
        "appointments": len(appts),
        "today": sum(1 for a in todays if a["status"] != "Cancelled"),
        "scheduled": sum(1 for a in appts if a["status"] == "Scheduled"),
        "byStatus": dict(Counter(a["status"] for a in appts)),
        "byDepartment": dict(Counter(a["department"] for a in active)),
        "next7Days": [{"date": d, "count": per_day.get(d, 0)} for d in next7],
        "todayQueue": [
            {"token": i + 1, "time": a["time"], "patientName": a["patientName"], "doctorName": a["doctorName"],
             "department": a["department"], "status": a["status"]}
            for i, a in enumerate(todays)
        ],
        "date": today,
    }


def seed():
    doctors = [
        {"doctorId": "D001", "name": "Dr. Anita Rao", "department": "Cardiology", "phone": "9000000001"},
        {"doctorId": "D002", "name": "Dr. Vikram Shah", "department": "Orthopaedics", "phone": "9000000002"},
        {"doctorId": "D003", "name": "Dr. Meera Iyer", "department": "Paediatrics", "phone": "9000000003"},
        {"doctorId": "D004", "name": "Dr. Arjun Nair", "department": "Neurology", "phone": "9000000004"},
    ]
    patients = [
        {"patientId": "P001", "name": "Ravi Kumar", "age": 45, "gender": "Male", "phone": "9876500001", "bloodGroup": "B+"},
        {"patientId": "P002", "name": "Priya Sharma", "age": 32, "gender": "Female", "phone": "9876500002", "bloodGroup": "O+"},
        {"patientId": "P003", "name": "Karthik R", "age": 8, "gender": "Male", "phone": "9876500003", "bloodGroup": "A+"},
        {"patientId": "P004", "name": "Lakshmi Devi", "age": 67, "gender": "Female", "phone": "9876500004", "bloodGroup": "AB+"},
    ]
    for d in doctors:
        create_doctor(d)
    for p in patients:
        create_patient(p)
    today = datetime.now(IST).date()
    plan = [
        ("P001", "D001", 0, "10:00", "Chest pain follow-up"),
        ("P003", "D003", 0, "11:30", "Fever"),
        ("P004", "D002", 0, "15:00", "Knee pain"),
        ("P002", "D004", 1, "09:30", "Migraine review"),
        ("P001", "D001", 2, "12:00", "ECG report review"),
        ("P004", "D004", 3, "16:00", "Dizziness"),
    ]
    booked = 0
    for pid, did, offset, time, reason in plan:
        try:
            create_appointment({"patientId": pid, "doctorId": did, "date": (today + timedelta(days=offset)).isoformat(),
                                "time": time, "reason": reason}, allow_past=True)
            booked += 1
        except Conflict:
            pass  # already seeded earlier
    return {"message": "Sample data loaded", "doctors": len(doctors), "patients": len(patients), "appointmentsAdded": booked}


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "Invalid JSON body"})

    try:
        if route == "GET /patients":
            return respond(200, list_patients())
        if route == "POST /patients":
            return respond(201, create_patient(body))
        if route == "GET /patients/{id}/appointments":
            return respond(200, patient_appointments(params["id"]))
        if route == "GET /doctors":
            return respond(200, list_doctors())
        if route == "POST /doctors":
            return respond(201, create_doctor(body))
        if route == "GET /doctors/{id}/appointments":
            return respond(200, doctor_appointments(params["id"]))
        if route == "POST /appointments":
            return respond(201, create_appointment(body))
        if route == "PATCH /appointments":
            return respond(200, update_appointment(body))
        if route == "GET /stats":
            return respond(200, stats())
        if route == "POST /seed":
            return respond(201, seed())
        return respond(404, {"error": f"Route not found: {route}"})
    except Conflict as e:
        return respond(409, {"error": str(e)})
    except ValueError as e:
        return respond(400, {"error": str(e)})
    except Exception as e:
        print("ERROR", repr(e))
        return respond(500, {"error": "Internal server error", "detail": str(e)})
