"""
api.py — FastAPI REST API for your movie database
"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, func
from typing import Optional

from database import init_db, get_db, Movie, Series

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title       = "Movie API",
    description = "Movies & Series with direct download links",
    version     = "1.0.0",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Serializers ───────────────────────────────────────────────────────────────
def movie_to_dict(m: Movie):
    return {
        "id":             m.id,
        "type":           "movie",
        "title":          m.title,
        "year":           m.year,
        "genre":          m.genre,
        "category":       m.category,
        "description":    m.description,
        "filesize":       m.filesize,
        "poster":         m.poster,
        "page_url":       m.page_url,
        "source_site":    m.source_site,
        "download_links": [
            dl for dl in (m.download_links or [])
            if dl.get("status") != "dead"
        ],
        "all_links":       m.download_links or [],
        "link_checked_at": m.link_checked_at.isoformat() if m.link_checked_at else None,
        "created_at":      m.created_at.isoformat() if m.created_at else None,
        "updated_at":      m.updated_at.isoformat() if m.updated_at else None,
    }

def series_to_dict(s: Series):
    return {
        "id":          s.id,
        "type":        "series",
        "title":       s.title,
        "year":        s.year,
        "genre":       s.genre,
        "category":    s.category,
        "description": s.description,
        "poster":      s.poster,
        "page_url":    s.page_url,
        "source_site": s.source_site,
        "seasons":     s.seasons or [],
        "created_at":  s.created_at.isoformat() if s.created_at else None,
        "updated_at":  s.updated_at.isoformat() if s.updated_at else None,
    }

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "name":    "Movie API",
        "version": "1.0.0",
        "endpoints": {
            "movies":         "/movies?page=1&limit=20",
            "movie_by_id":    "/movies/{id}",
            "movie_search":   "/movies/search?q=avengers",
            "movie_category": "/movies/category/Hollywood",
            "series":         "/series?page=1&limit=20",
            "series_by_id":   "/series/{id}",
            "series_search":  "/series/search?q=money+heist",
            "search_all":     "/search?q=anything",
            "stats":          "/stats",
            "health":         "/health",
        }
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/stats")
def stats(db: Session = Depends(get_db)):
    movie_count  = db.query(func.count(Movie.id)).filter(Movie.is_active == True).scalar()
    series_count = db.query(func.count(Series.id)).filter(Series.is_active == True).scalar()
    nkiri_count  = db.query(func.count(Movie.id)).filter(Movie.source_site == "nkiri").scalar()
    jarocks_count= db.query(func.count(Movie.id)).filter(Movie.source_site == "9jarocks").scalar()
    return {
        "total_movies":  movie_count,
        "total_series":  series_count,
        "total_items":   movie_count + series_count,
        "by_source": {
            "nkiri":    nkiri_count,
            "9jarocks": jarocks_count,
        }
    }

# ── Movies ────────────────────────────────────────────────────────────────────
@app.get("/movies")
def list_movies(
    page:   int           = Query(1,  ge=1),
    limit:  int           = Query(20, ge=1, le=100),
    source: Optional[str] = Query(None),
    db:     Session       = Depends(get_db),
):
    q = db.query(Movie).filter(Movie.is_active == True)
    if source:
        q = q.filter(Movie.source_site == source)
    total = q.count()
    items = q.order_by(Movie.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [movie_to_dict(m) for m in items],
    }

@app.get("/movies/search")
def search_movies(
    q:     str     = Query(..., min_length=1),
    page:  int     = Query(1,  ge=1),
    limit: int     = Query(20, ge=1, le=100),
    db:    Session = Depends(get_db),
):
    query = db.query(Movie).filter(
        Movie.is_active == True,
        or_(Movie.title.ilike(f"%{q}%"), Movie.description.ilike(f"%{q}%"),
            Movie.genre.ilike(f"%{q}%"))
    )
    total = query.count()
    items = query.order_by(Movie.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "query": q, "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [movie_to_dict(m) for m in items],
    }

@app.get("/movies/category/{category}")
def movies_by_category(
    category: str,
    page:     int     = Query(1,  ge=1),
    limit:    int     = Query(20, ge=1, le=100),
    db:       Session = Depends(get_db),
):
    query = db.query(Movie).filter(
        Movie.is_active == True, Movie.category.ilike(f"%{category}%"))
    total = query.count()
    items = query.order_by(Movie.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "category": category, "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [movie_to_dict(m) for m in items],
    }

@app.get("/movies/{movie_id}")
def get_movie(movie_id: int, db: Session = Depends(get_db)):
    movie = db.query(Movie).filter(Movie.id == movie_id, Movie.is_active == True).first()
    if not movie:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie_to_dict(movie)

# ── Series ────────────────────────────────────────────────────────────────────
@app.get("/series")
def list_series(
    page:   int           = Query(1,  ge=1),
    limit:  int           = Query(20, ge=1, le=100),
    source: Optional[str] = Query(None),
    db:     Session       = Depends(get_db),
):
    q = db.query(Series).filter(Series.is_active == True)
    if source:
        q = q.filter(Series.source_site == source)
    total = q.count()
    items = q.order_by(Series.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [series_to_dict(s) for s in items],
    }

@app.get("/series/search")
def search_series(
    q:     str     = Query(..., min_length=1),
    page:  int     = Query(1,  ge=1),
    limit: int     = Query(20, ge=1, le=100),
    db:    Session = Depends(get_db),
):
    query = db.query(Series).filter(
        Series.is_active == True,
        or_(Series.title.ilike(f"%{q}%"), Series.description.ilike(f"%{q}%"),
            Series.genre.ilike(f"%{q}%"))
    )
    total = query.count()
    items = query.order_by(Series.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "query": q, "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [series_to_dict(s) for s in items],
    }

@app.get("/series/category/{category}")
def series_by_category(
    category: str,
    page:     int     = Query(1,  ge=1),
    limit:    int     = Query(20, ge=1, le=100),
    db:       Session = Depends(get_db),
):
    query = db.query(Series).filter(
        Series.is_active == True, Series.category.ilike(f"%{category}%"))
    total = query.count()
    items = query.order_by(Series.created_at.desc()).offset((page-1)*limit).limit(limit).all()
    return {
        "category": category, "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": [series_to_dict(s) for s in items],
    }

@app.get("/series/{series_id}")
def get_series(series_id: int, db: Session = Depends(get_db)):
    s = db.query(Series).filter(Series.id == series_id, Series.is_active == True).first()
    if not s:
        raise HTTPException(status_code=404, detail="Series not found")
    return series_to_dict(s)

# ── Combined search ───────────────────────────────────────────────────────────
@app.get("/search")
def search_all(
    q:     str     = Query(..., min_length=1),
    page:  int     = Query(1,  ge=1),
    limit: int     = Query(20, ge=1, le=100),
    db:    Session = Depends(get_db),
):
    movies = db.query(Movie).filter(
        Movie.is_active == True,
        or_(Movie.title.ilike(f"%{q}%"), Movie.description.ilike(f"%{q}%"))
    ).all()
    series = db.query(Series).filter(
        Series.is_active == True,
        or_(Series.title.ilike(f"%{q}%"), Series.description.ilike(f"%{q}%"))
    ).all()
    results = sorted(
        [movie_to_dict(m) for m in movies] + [series_to_dict(s) for s in series],
        key=lambda x: x["title"]
    )
    total = len(results)
    start = (page - 1) * limit
    return {
        "query": q, "page": page, "limit": limit, "total": total,
        "total_pages": (total + limit - 1) // limit,
        "results": results[start:start+limit],
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
