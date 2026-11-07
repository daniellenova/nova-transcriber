class MediaError(ValueError):
    pass


class EmptyFileError(MediaError):
    def __init__(self):
        super().__init__("Файл пустой")


class UnsupportedFormatError(MediaError):
    def __init__(self):
        super().__init__("Неподдерживаемый формат файла")


class UnreadableMediaError(MediaError):
    def __init__(self):
        super().__init__("Не удалось прочитать аудио или видео. Проверьте файл")
