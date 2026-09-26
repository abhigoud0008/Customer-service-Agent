import os
import firebase_admin
from firebase_admin import credentials, firestore

cred_path = os.path.join(os.path.dirname(__file__), "firebase_credentials.json")
print(f"Checking credentials at: {cred_path}")

try:
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    print("Attempting to write test ticket to Firestore...")
    doc_ref = db.collection("tickets").document()
    doc_ref.set({
        "customer_name": "Test User",
        "issue": "Checking connection",
        "status": "Open",
    })
    print(f" SUCCESS! Document written with ID: {doc_ref.id}")
except Exception as e:
    print(f"\n❌ FIREBASE ERROR DETECTED:\n{type(e)._name_}: {e}")