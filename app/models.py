from typing import Literal, Optional

from pydantic import BaseModel


class User(BaseModel):
    username: str
    role: Literal["read", "write", "admin"]


class FileInfo(BaseModel):
    path: str
    name: str
    is_dir: bool
    size: int
    updated_at: Optional[str] = None
    has_children: bool = False


class FileContent(BaseModel):
    path: str
    content: str
    frontmatter: dict = {}


class SearchResult(BaseModel):
    path: str
    title: str
    snippet: str
    score: float


class WriteFileRequest(BaseModel):
    path: str
    content: str


class AppendFileRequest(BaseModel):
    path: str
    content: str


class CreateFolderRequest(BaseModel):
    path: str


class SearchRequest(BaseModel):
    query: str


class CreateNoteRequest(BaseModel):
    title: str
    content: str
    tags: list[str] = []


class MoveRequest(BaseModel):
    src: str
    dst: str


class CreateUserRequest(BaseModel):
    username: str
    key: str
    role: Literal["read", "write", "admin"]
