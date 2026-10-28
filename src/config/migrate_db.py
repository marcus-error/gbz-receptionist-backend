import sqlite3
import os

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "receptionist.db")

if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    columns_to_add = [
        ("users", "organization_id", "INTEGER"),
        ("call_logs", "organization_id", "INTEGER DEFAULT 1"),
        ("appointments", "organization_id", "INTEGER DEFAULT 1"),
        ("knowledge_documents", "organization_id", "INTEGER DEFAULT 1"),
        ("knowledge_documents", "receptionist_id", "TEXT DEFAULT 'default'"),
        ("staff", "organization_id", "INTEGER DEFAULT 1"),
        ("notification_logs", "organization_id", "INTEGER DEFAULT 1"),
        ("whatsapp_configs", "organization_id", "INTEGER DEFAULT 1"),
        ("email_configs", "organization_id", "INTEGER DEFAULT 1"),
        ("email_configs", "staff_template", "TEXT"),
        ("crm_connections", "organization_id", "INTEGER DEFAULT 1"),
        ("crm_sync_settings", "organization_id", "INTEGER DEFAULT 1"),
        ("crm_automation_rules", "organization_id", "INTEGER DEFAULT 1"),
        ("crm_field_mappings", "organization_id", "INTEGER DEFAULT 1"),
        ("crm_sync_logs", "organization_id", "INTEGER DEFAULT 1"),
        ("calendar_configs", "organization_id", "INTEGER DEFAULT 1"),
        ("app_settings", "organization_id", "INTEGER DEFAULT 1")
    ]

    for table, col, col_type in columns_to_add:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")
            print(f"Added column {col} to {table}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower() or "no such table" in str(e).lower():
                pass
            else:
                print(f"Migration note for {table}.{col}: {e}")

    conn.commit()
    conn.close()
    print("Database migration completed successfully!")
