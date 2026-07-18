from fastapi import FastAPI, Depends, HTTPException, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from database import engine, Base, get_db
import models
from datetime import datetime, date, time

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def get_current_user(db: Session = Depends(get_db)):
    mock_student = db.query(models.Student).filter_by(email="sarthak@iiitkottayam.ac.in").first()
    if not mock_student:
        mock_student = models.Student(
            name="Sarthak Pandey", 
            email="sarthak@iiitkottayam.ac.in", 
            phone="+919876543210"
        )
        db.add(mock_student)
        db.commit()
        db.refresh(mock_student)
    return mock_student

@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    now = datetime.now()
    
    # 1. Fetch all rides that depart in the future from the database
    future_pools = db.query(models.Pool).filter(
        models.Pool.departure_time > now
    ).order_by(models.Pool.departure_time.asc()).all()
    
    # 2. Safely filter out the full pools using the Python property in memory
    active_pools = [pool for pool in future_pools if pool.remaining_seats > 0]
    
    return templates.TemplateResponse("index.html", {"request": request, "pools": active_pools, "current_user": current_user})

@app.get("/pool/create")
def create_pool_page(request: Request):
    return templates.TemplateResponse("create.html", {"request": request})

@app.post("/pool/create")
def handle_create_pool(
    source: str = Form(...),
    destination: str = Form(...),
    time_str: str = Form(...), 
    total_fare: int = Form(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    parsed_time = time.fromisoformat(time_str)
    today_date = date.today()
    departure = datetime.combine(today_date, parsed_time)
    
    if departure < datetime.now():
        raise HTTPException(status_code=400, detail="Cannot create a pool for a time that has already passed today.")

    new_pool = models.Pool(
        creator_id=current_user.id,
        source=source,
        destination=destination,
        departure_time=departure,
        total_fare=total_fare
    )
    db.add(new_pool)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/pool/{pool_id}/join")
def join_pool(pool_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    pool = db.query(models.Pool).filter(models.Pool.id == pool_id).first()
    
    if not pool or pool.departure_time <= datetime.now():
        raise HTTPException(status_code=400, detail="This ride has already departed.")
        
    if pool.creator_id == current_user.id or current_user in pool.members or pool.remaining_seats <= 0:
        raise HTTPException(status_code=400, detail="Invalid operation")
        
    pool.members.append(current_user)
    db.commit()
    return RedirectResponse(url="/", status_code=303)