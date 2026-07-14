from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field


MAX_PATH_LENGTH = 1024
MAX_CONTENT_LENGTH = 2 * 1024 * 1024
MAX_QUERY_LENGTH = 512
MAX_TITLE_LENGTH = 200
MAX_TAGS = 50
MAX_TAG_LENGTH = 100

PathValue = Annotated[str, Field(min_length=1, max_length=MAX_PATH_LENGTH)]
ContentValue = Annotated[str, Field(max_length=MAX_CONTENT_LENGTH)]
TagValue = Annotated[str, Field(min_length=1, max_length=MAX_TAG_LENGTH)]


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
    frontmatter: dict = Field(default_factory=dict)
    revision: Optional[int] = None


class SearchResult(BaseModel):
    path: str
    title: str
    snippet: str
    score: float


class WriteFileRequest(BaseModel):
    path: PathValue
    content: ContentValue
    expected_revision: Optional[int] = Field(default=None, ge=0)


class AppendFileRequest(BaseModel):
    path: PathValue
    content: ContentValue


class CreateFolderRequest(BaseModel):
    path: PathValue


class SearchRequest(BaseModel):
    query: Annotated[str, Field(max_length=MAX_QUERY_LENGTH)]


class CreateNoteRequest(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_LENGTH)]
    content: ContentValue
    tags: list[TagValue] = Field(default_factory=list, max_length=MAX_TAGS)


class MoveRequest(BaseModel):
    src: PathValue
    dst: PathValue


class UpdateFrontmatterRequest(BaseModel):
    path: PathValue
    updates: dict = Field(max_length=100)


class CreateUserRequest(BaseModel):
    username: str
    key: str
    role: Literal["read", "write", "admin"]
