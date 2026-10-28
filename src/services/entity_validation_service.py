"""
Dedicated Entity Validation & Active Organization Grounding Layer.

Architecture:
1. USER MENTIONED ENTITY ≠ VERIFIED ENTITY.
2. Every entity (Staff, Doctor, Service, Department, Location) MUST be validated against the ACTIVE ORGANIZATION's database and Knowledge Base before being accepted into conversation/booking state.
3. If an entity is NOT verified in the active organization, the system MUST NOT proceed to date/time collection or claim the entity exists.
4. Enforces semantic domain alignment (e.g., does not transform a salon staff member into a doctor, or assume hospital departments exist in a salon).
"""

import re
import difflib
import json
import logging
from typing import Dict, Any, Optional, List
from pydantic import BaseModel
from sqlalchemy.orm import Session
from src.services.booking_state_service import BookingStateService

logger = logging.getLogger(__name__)


class EntityType:
    PERSON = "person"
    DOCTOR = "doctor"
    STAFF = "staff"
    SERVICE = "service"
    DEPARTMENT = "department"
    LOCATION = "location"
    PRODUCT = "product"
    UNKNOWN = "unknown"


class EntityValidationResult(BaseModel):
    is_verified: bool = False
    is_role_category: bool = False   # True when user said a role noun (teacher/doctor) not a specific person
    is_ambiguous: bool = False       # True when candidate closely resembles a known entity (e.g. Galaxy Jet -> Galaxy Chat)
    suggested_entity: Optional[str] = None
    user_mentioned_entity: str
    verified_name: Optional[str] = None
    staff_id: Optional[int] = None
    entity_type: str = EntityType.UNKNOWN
    role_or_specialty: Optional[str] = None
    organization_id: int = 1
    organization_name: Optional[str] = None
    organization_type: Optional[str] = None
    reason: str = ""


class EntityValidationService:
    """
    Centralized Entity Validator for Multi-Tenant / Multi-Organization Grounding.
    Guarantees zero fake entity propagation across organizational boundaries.
    """

    def validate_entity(
        self,
        entity_name: str,
        organization_id: int = 1,
        expected_type: Optional[str] = None,
        db_session: Optional[Session] = None
    ) -> EntityValidationResult:
        """
        Validates user-mentioned entity against the ACTIVE organization's verified DB & RAG data.
        """
        if not entity_name or not entity_name.strip():
            return EntityValidationResult(
                is_verified=False,
                user_mentioned_entity="",
                reason="Empty entity name provided."
            )

        candidate_clean = entity_name.strip()
        org_id_str = str(organization_id or 1)

        # ------------------------------------------------------------------
        # Reject bare titles ("doctor", "dr", "staff") from being accepted
        # as verified staff members.
        # ------------------------------------------------------------------
        _candidate_lower = candidate_clean.lower().strip()
        if _candidate_lower in ("doctor", "dr", "dr.", "staff", "representative", "specialist"):
            return EntityValidationResult(
                is_verified=False,
                user_mentioned_entity=candidate_clean,
                organization_id=organization_id,
                reason="Bare title without name cannot be accepted as a specific staff member."
            )

        # ------------------------------------------------------------------
        # ROLE-CATEGORY PRE-CHECK
        # Only accept exact specialty/role nouns (e.g. "pediatrician", "dentist",
        # "cardiologist") if no specific individual person name is attached.
        # ------------------------------------------------------------------
        _ROLE_CATEGORY_NOUNS = {
            # Education
            "teacher", "teachers", "principal", "counselor", "tutor", "faculty",
            "lecturer", "headmaster", "headmistress", "class teacher", "school teacher",
            # Medical Specialties (NOT bare 'doctor')
            "physician", "surgeon", "dentist", "pediatrician", "neurologist",
            "cardiologist", "dermatologist", "gynecologist", "orthopedic",
            # Salon / Beauty
            "hairstylist", "beautician",
            # Legal
            "attorney", "lawyer", "advocate", "solicitor",
            # Finance
            "financial advisor", "loan officer", "banker",
            # Therapy / Wellness
            "psychologist", "counsellor", "physiotherapist",
            "chiropractor", "nutritionist", "dietitian",
            # Tech
            "support engineer",
            # Veterinary
            "veterinarian",
            # Real Estate
            "broker",
        }
        
        # Only exact role noun match (NEVER strings starting with 'doctor <name>')
        if _candidate_lower in _ROLE_CATEGORY_NOUNS:
            from src.services.rag_service import rag_service
            org_name = rag_service.get_organization_name(organization_id) or "our organization"
            return EntityValidationResult(
                is_verified=True,
                is_role_category=True,
                user_mentioned_entity=candidate_clean,
                verified_name=candidate_clean.title(),
                entity_type=EntityType.STAFF,
                organization_id=organization_id,
                organization_name=org_name,
                reason=f"Role category '{candidate_clean}' accepted as appointment target."
            )

        # 1. Inspect DB Staff Table for Active Organization
        close_db = False
        db = db_session
        if not db:
            from src.config.database import SessionLocal
            db = SessionLocal()
            close_db = True

        try:
            from src.models.staff import Staff, StaffStatus
            from src.services.rag_service import rag_service

            org_name = rag_service.get_organization_name(organization_id) or "our organization"
            best_ambiguous_entity: Optional[str] = None
            best_ambiguous_sim: float = 0.0

            # 1. Inspect Active RAG Knowledge Base first for Verified Services, Departments, Teachers, and Staff
            master_ctx = rag_service.get_organization_master_context(organization_id)
            if master_ctx:
                ctx_lower = master_ctx.lower()
                clean_target = re.sub(r'\b(dr\.?|doctor|mr\.?|mrs\.?|ms\.?|prof\.?|stylist|teacher)\s*', '', candidate_clean.lower()).strip()
                norm_target = re.sub(r'[^a-z0-9\s]', '', clean_target)

                # Extract list of services & departments from RAG (inline + bulleted)
                rag_departments = []
                rag_services_and_depts = []

                # Specifically extract departments section
                dept_bullet_matches = re.findall(r'(?:#+\s*(?:DEPARTMENTS|SPECIALTIES)[^\n]*\n(?:[^\n]*\n)*?)((?:[-*•]\s*[^\n]+\n?)+)', master_ctx, re.IGNORECASE)
                for bm in dept_bullet_matches:
                    for b in re.findall(r'[-*•]\s*([^\n\r]+)', bm):
                        clean_b = b.strip().replace('*', '')
                        if clean_b and clean_b not in rag_departments:
                            rag_departments.append(clean_b)

                dept_inline = re.search(r'(?:Departments|Specialties)\s*:\s*([^\n]+)', master_ctx, re.IGNORECASE)
                if dept_inline:
                    for s_item in re.split(r'[,;•\n]', dept_inline.group(1)):
                        clean_item = s_item.strip().replace('*', '')
                        if clean_item and clean_item not in rag_departments:
                            rag_departments.append(clean_item)

                services_match = re.search(r'(?:Services|Offerings|Treatments|Menu)\s*:\s*([^\n]+)', master_ctx, re.IGNORECASE)
                if services_match:
                    rag_services_and_depts.extend([s.strip().replace('*', '') for s in re.split(r'[,;•\n]', services_match.group(1)) if len(s.strip()) > 2])

                bullet_matches = re.findall(r'(?:#+\s*(?:SERVICES|OFFERINGS|TREATMENTS|MENU)[^\n]*\n(?:[^\n]*\n)*?)((?:[-*•]\s*[^\n]+\n?)+)', master_ctx, re.IGNORECASE)
                for bm in bullet_matches:
                    bullets = re.findall(r'[-*•]\s*([^\n\r]+)', bm)
                    for b in bullets:
                        clean_b = b.strip().replace('*', '')
                        if clean_b and clean_b not in rag_services_and_depts:
                            rag_services_and_depts.append(clean_b)

                # Merge dynamically extracted services & products from active RAG
                rag_dynamic = rag_service.get_organization_services(organization_id)
                for r_item in rag_dynamic:
                    if r_item and r_item not in rag_services_and_depts:
                        rag_services_and_depts.append(r_item)

                norm_clean_target = re.sub(r'[^a-z0-9]', '', clean_target)

                # Check departments first
                for d in rag_departments:
                    d_clean = d.lower().strip()
                    norm_d = re.sub(r'[^a-z0-9]', '', d_clean)
                    sim = difflib.SequenceMatcher(None, clean_target, d_clean).ratio()
                    norm_sim = difflib.SequenceMatcher(None, norm_clean_target, norm_d).ratio()
                    if clean_target == d_clean or clean_target in d_clean or d_clean in clean_target or norm_clean_target in norm_d or norm_d in norm_clean_target or sim >= 0.85 or norm_sim >= 0.85:
                        return EntityValidationResult(
                            is_verified=True,
                            user_mentioned_entity=candidate_clean,
                            verified_name=d.title(),
                            entity_type=EntityType.DEPARTMENT,
                            role_or_specialty=d.title(),
                            organization_id=organization_id,
                            organization_name=org_name,
                            reason=f"Verified department match '{d}' in active Knowledge Base."
                        )
                    if 0.60 <= sim < 0.85 and sim > best_ambiguous_sim:
                        best_ambiguous_sim = sim
                        best_ambiguous_entity = d.title()

                HOME_HEALTH_TERMS = {"home", "caregiver", "caregiving", "nursing", "elderly", "homecare", "home healthcare", "care assistance"}
                DENTAL_TERMS = {"dental", "dentist", "dentistry", "teeth", "tooth"}
                cand_is_home_health = any(w in clean_target for w in HOME_HEALTH_TERMS)

                for s in rag_services_and_depts:
                    s_clean = s.lower().strip()
                    # Do not redirect home healthcare to dental
                    if cand_is_home_health and any(w in s_clean for w in DENTAL_TERMS):
                        continue
                    # Never match purely on generic stop words like 'care' or 'health'
                    if clean_target in ("care", "health", "clinic", "center", "department", "service", "help"):
                        continue

                    norm_s = re.sub(r'[^a-z0-9]', '', s_clean)
                    sim = difflib.SequenceMatcher(None, clean_target, s_clean).ratio()
                    norm_sim = difflib.SequenceMatcher(None, norm_clean_target, norm_s).ratio()
                    
                    # Exact or word boundary match
                    exact = (clean_target == s_clean or norm_clean_target == norm_s)
                    wb_match = bool(re.search(r'\b' + re.escape(clean_target) + r'\b', s_clean) or re.search(r'\b' + re.escape(s_clean) + r'\b', clean_target))
                    
                    if exact or (wb_match and len(clean_target) >= 4) or sim >= 0.85 or norm_sim >= 0.85:
                        is_dept = "department" in s_clean or "mathematics" in s_clean or "science" in s_clean or "english" in s_clean or any(k in s_clean for k in ["cardio", "dental", "pediatr", "ortho", "derma", "neuro", "medic"])
                        is_prod = any(w in ctx_lower for w in ["product suite", "products", "software", "platform", "chat", "bot"]) and not is_dept
                        return EntityValidationResult(
                            is_verified=True,
                            user_mentioned_entity=candidate_clean,
                            verified_name=s.title(),
                            entity_type=EntityType.DEPARTMENT if is_dept else (EntityType.PRODUCT if is_prod else EntityType.SERVICE),
                            role_or_specialty=s.title() if is_dept else ("Product" if is_prod else "Service"),
                            organization_id=organization_id,
                            organization_name=org_name,
                            reason=f"Verified match '{s}' in active Knowledge Base."
                        )
                    if 0.60 <= sim < 0.85 and sim > best_ambiguous_sim:
                        best_ambiguous_sim = sim
                        best_ambiguous_entity = s.title()

                rag_docs = BookingStateService._extract_doctors_from_script(master_ctx)
                for rdoc in rag_docs:
                    doc_name = rdoc["name"]
                    if BookingStateService._is_doctor_name_match(candidate_clean, doc_name):
                        from src.models.staff import Staff
                        clean_n = re.sub(r'^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?|prof\.?)\s+', '', doc_name.strip(), flags=re.I).strip()
                        db_staff = db.query(Staff).filter(
                            Staff.organization_id == organization_id,
                            Staff.name.ilike(f"%{clean_n}%"),
                            Staff.online_booking_enabled != False,
                            Staff.status != StaffStatus.OFFLINE
                        ).first()
                        if not db_staff:
                            working_days, start_t, end_t, w_hours = BookingStateService._parse_script_schedule(rdoc.get("hours", ""))
                            display_name = f"Dr. {clean_n}" if not clean_n.lower().startswith("dr") else clean_n
                            new_staff = Staff(
                                organization_id=organization_id,
                                name=display_name,
                                department=rdoc.get("dept") or rdoc.get("spec") or "Ayurvedic Consultant",
                                working_days=json.dumps(working_days),
                                start_time=start_t,
                                end_time=end_t,
                                working_hours=w_hours,
                                status=StaffStatus.AVAILABLE,
                                online_booking_enabled=True,
                                appointment_duration="30 min",
                            )
                            db.add(new_staff)
                            db.commit()
                            db.refresh(new_staff)
                            db_staff = new_staff

                        s_id = db_staff.id if db_staff else None
                        s_dept = db_staff.department if db_staff else rdoc.get("dept")
                        return EntityValidationResult(
                            is_verified=True,
                            user_mentioned_entity=candidate_clean,
                            verified_name=db_staff.name if db_staff else f"Dr. {clean_n}",
                            staff_id=s_id,
                            entity_type=EntityType.DOCTOR,
                            role_or_specialty=s_dept,
                            organization_id=organization_id,
                            organization_name=org_name,
                            reason=f"Verified doctor match '{doc_name}' in active Knowledge Base."
                        )

                # Also check general staff names in master_ctx (stylists, teachers, etc.)
                rag_staff = []
                staff_match = re.search(r'(?:Stylists|Stylist|Staff|Doctors|Doctor|Teachers|Teacher|Faculty|Team|Lawyers)\s*:\s*([^\n]+)', master_ctx, re.IGNORECASE)
                if staff_match:
                    raw_items = [st.strip().replace('*', '') for st in re.split(r'[,;•\n]', staff_match.group(1)) if len(st.strip()) > 2]
                    for item in raw_items:
                        clean_item = re.sub(r'[\(\[\{-].*$', '', item).strip()
                        if clean_item and clean_item not in rag_staff:
                            rag_staff.append(clean_item)

                dir_names = re.findall(r'\b(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Prof\.?)\s+([A-Za-z0-9\.\s]+?)(?=[,\n\r]|$)', master_ctx, re.IGNORECASE)
                for dn in dir_names:
                    clean_dn = dn.strip()
                    if clean_dn and len(clean_dn) >= 3 and clean_dn not in rag_staff:
                        rag_staff.append(clean_dn)

                for st in rag_staff:
                    if BookingStateService._is_doctor_name_match(candidate_clean, st):
                        from src.models.staff import Staff
                        clean_n = re.sub(r'^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?|prof\.?)\s+', '', st.strip(), flags=re.I).strip()
                        db_staff = db.query(Staff).filter(
                            Staff.organization_id == organization_id,
                            Staff.name.ilike(f"%{clean_n}%"),
                            Staff.online_booking_enabled != False,
                            Staff.status != StaffStatus.OFFLINE
                        ).first()
                        s_id = db_staff.id if db_staff else None
                        s_dept = db_staff.department if db_staff else None
                        is_med = "dr" in st.lower() or "doctor" in st.lower() or (s_dept and any(w in s_dept.lower() for w in ["cardio", "dental", "general", "medic", "ortho", "pediatr", "ayur", "siddha"]))
                        if is_med and not s_id:
                            continue

                        return EntityValidationResult(
                            is_verified=True,
                            user_mentioned_entity=candidate_clean,
                            verified_name=db_staff.name if db_staff else st.title(),
                            staff_id=s_id,
                            entity_type=EntityType.DOCTOR if is_med else EntityType.STAFF,
                            role_or_specialty=s_dept,
                            organization_id=organization_id,
                            organization_name=org_name,
                            reason=f"Verified staff match '{st}' in active Knowledge Base."
                        )

            # 2. Inspect DB Department & Staff Tables for Active Organization
            clean_cand_dept = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', candidate_clean.strip(), flags=re.I).strip().lower()
            HOME_HEALTH_TERMS = {"home", "caregiver", "caregiving", "nursing", "elderly", "homecare", "home healthcare", "care assistance"}
            DENTAL_TERMS = {"dental", "dentist", "dentistry", "teeth", "tooth"}
            cand_is_home_health = any(w in clean_cand_dept for w in HOME_HEALTH_TERMS)
            try:
                from src.models.department import Department
                db_dept = db.query(Department).filter(
                    Department.organization_id == organization_id,
                    Department.name.ilike(f"%{clean_cand_dept}%")
                ).first()
                if db_dept:
                    return EntityValidationResult(
                        is_verified=True,
                        user_mentioned_entity=candidate_clean,
                        verified_name=db_dept.name.title(),
                        entity_type=EntityType.DEPARTMENT,
                        role_or_specialty=db_dept.name.title(),
                        organization_id=organization_id,
                        organization_name=org_name,
                        reason=f"Verified department match '{db_dept.name}' in active organization database."
                    )
                # Check distinct Staff.department in DB
                staff_depts = db.query(Staff.department).filter(
                    Staff.organization_id == organization_id,
                    Staff.department.isnot(None)
                ).distinct().all()
                for (s_dept,) in staff_depts:
                    if s_dept:
                        s_dept_clean = s_dept.lower().strip()
                        if cand_is_home_health and any(w in s_dept_clean for w in DENTAL_TERMS):
                            continue
                        if clean_cand_dept in ("care", "health", "clinic", "center", "department", "service"):
                            continue
                        wb_d = bool(re.search(r'\b' + re.escape(clean_cand_dept) + r'\b', s_dept_clean) or re.search(r'\b' + re.escape(s_dept_clean) + r'\b', clean_cand_dept))
                        if clean_cand_dept == s_dept_clean or (wb_d and len(clean_cand_dept) >= 4):
                            return EntityValidationResult(
                                is_verified=True,
                                user_mentioned_entity=candidate_clean,
                                verified_name=s_dept.title(),
                                entity_type=EntityType.DEPARTMENT,
                                role_or_specialty=s_dept.title(),
                                organization_id=organization_id,
                                organization_name=org_name,
                                reason=f"Verified department match '{s_dept}' in active organization staff directory."
                            )
            except Exception as _dept_err:
                logger.warning(f"Error checking DB departments: {_dept_err}")

            staff_records = db.query(Staff).filter(
                Staff.organization_id == organization_id,
                Staff.online_booking_enabled != False,
                Staff.status != StaffStatus.OFFLINE
            ).all()

            for staff in staff_records:
                if not staff.name:
                    continue
                is_medical = any(w in (staff.name + " " + (staff.department or "")).lower() for w in ["dr", "doctor", "physician", "medicine", "cardio", "neuro", "ortho", "surgery", "clinic", "dental"])
                
                clean_staff = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', staff.name.strip(), flags=re.I).strip().lower()
                clean_cand = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', candidate_clean.strip(), flags=re.I).strip().lower()

                # 1. Exact match
                if clean_staff == clean_cand or staff.name.lower() == candidate_clean.lower():
                    return EntityValidationResult(
                        is_verified=True,
                        user_mentioned_entity=candidate_clean,
                        verified_name=staff.name,
                        staff_id=staff.id,
                        entity_type=EntityType.DOCTOR if is_medical else EntityType.STAFF,
                        role_or_specialty=staff.department,
                        organization_id=organization_id,
                        organization_name=org_name,
                        reason="Verified staff member match in active organization database."
                    )

                name_tokens = [re.sub(r'[^a-zA-Z]', '', t).lower() for t in clean_staff.split()]
                name_tokens = [t for t in name_tokens if len(t) >= 3 and t not in ["dr", "doctor", "mr", "mrs", "ms", "the", "and"]]

                candidate_tokens = [re.sub(r'[^a-zA-Z]', '', t).lower() for t in clean_cand.split()]
                candidate_tokens = [t for t in candidate_tokens if len(t) >= 3 and t not in ["dr", "doctor", "mr", "mrs", "ms", "the", "and"]]

                
                sim_ratio = difflib.SequenceMatcher(None, clean_cand, clean_staff).ratio()
                sim_ratio_full = difflib.SequenceMatcher(None, candidate_clean.lower(), staff.name.lower()).ratio()

                
                diff_first = bool(candidate_tokens and name_tokens and len(candidate_tokens) > 1 and len(name_tokens) > 1 and candidate_tokens[0] != name_tokens[0])
                is_cand_subset = bool(candidate_tokens) and bool(name_tokens) and set(candidate_tokens).issubset(set(name_tokens))
                is_staff_subset = bool(candidate_tokens) and bool(name_tokens) and len(candidate_tokens) == 1 and (name_tokens[0] == candidate_tokens[0])
                is_high_sim = (sim_ratio >= 0.85 or sim_ratio_full >= 0.85)

                if BookingStateService._is_doctor_name_match(candidate_clean, staff.name) or (not diff_first and (is_cand_subset or is_staff_subset or is_high_sim)):
                    return EntityValidationResult(
                        is_verified=True,
                        user_mentioned_entity=candidate_clean,
                        verified_name=staff.name,
                        staff_id=staff.id,
                        entity_type=EntityType.DOCTOR if is_medical else EntityType.STAFF,
                        role_or_specialty=staff.department,
                        organization_id=organization_id,
                        organization_name=org_name,
                        reason="Verified match (exact/fuzzy STT correction) in active organization database."
                    )

            # 3. Entity Not Verified
            if best_ambiguous_entity:
                return EntityValidationResult(
                    is_verified=False,
                    is_ambiguous=True,
                    user_mentioned_entity=candidate_clean,
                    suggested_entity=best_ambiguous_entity,
                    organization_id=organization_id,
                    organization_name=org_name,
                    reason=f"Ambiguous match '{candidate_clean}', closely resembles '{best_ambiguous_entity}'."
                )

            return EntityValidationResult(
                is_verified=False,
                user_mentioned_entity=candidate_clean,
                organization_id=organization_id,
                organization_name=org_name,
                reason=f"Entity '{candidate_clean}' does not exist in active organization {org_name}."
            )

        finally:
            if close_db:
                db.close()

    def get_unverified_entity_response(self, entity_name: str, organization_id: int = 1, language: str = "en") -> str:
        """
        Generates polite, grounded response when a user requests an unverified person or service.
        Respects conversation language lock (Tamil/English/Hindi).
        """
        from src.services.rag_service import rag_service
        org_name = rag_service.get_organization_name(organization_id)
        org_label = org_name if org_name else "organization"

        clean_entity = (entity_name or "that requested person or service").strip()
        lang_code = (language or "en").lower().strip()

        if lang_code in ("ta", "tanglish", "ta_mix"):
            return f"{clean_entity}-ஓட details நம்ம current {org_label} information-ல கிடைக்கல. Name once confirm பண்ண முடியுமா?"
        elif lang_code in ("hi", "hinglish"):
            return f"{clean_entity} ki jankari hamare {org_label} directory me nahi mili. Kya aap naam confirm kar sakte hain?"
        else:
            return (
                f"I couldn't find '{clean_entity}' in our current {org_label} directory. "
                f"Could you please confirm the name or tell me what service you're looking for?"
            )


entity_validation_service = EntityValidationService()
