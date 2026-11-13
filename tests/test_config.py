from app.core.config import Settings


def test_database_url_shape():
    s = Settings(db_user="u", db_password="p", db_host="h", db_port=1234, db_name="d")
    assert s.database_url == "mysql+pymysql://u:p@h:1234/d?charset=utf8mb4"
