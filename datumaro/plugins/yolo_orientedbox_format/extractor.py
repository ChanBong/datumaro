# Copyright (C) 2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import yaml

import os.path as osp
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple, Type, TypeVar, Union, Iterator

from datumaro.components.annotation import Annotation, AnnotationType, Bbox, LabelCategories
from datumaro.components.errors import (
    DatasetImportError,
    InvalidAnnotationError,
    UndeclaredLabelError,
)
from datumaro.components.extractor import DatasetItem, Extractor, Importer, SourceExtractor
from datumaro.components.format_detection import FormatDetectionContext
from datumaro.components.media import Image
from datumaro.util.image import (
    DEFAULT_IMAGE_META_FILE_NAME,
    IMAGE_EXTENSIONS,
    ImageMeta,
    load_image,
    load_image_meta_file,
)
from datumaro.util.meta_file_util import has_meta_file, parse_meta_file
from datumaro.util.os_util import split_path, extract_subset_name_from_parent, find_files
import numpy as np
import cv2

from .format import YoloOrientedboxPath

T = TypeVar("T")

def xyxyxyxy2xywhr(cnt):
    if cnt.dtype != np.int32:
        cnt = cnt.astype(np.int32)
    return cv2.minAreaRect(cnt)


class YoloOrientedboxExtractor(SourceExtractor):
    class Subset(Extractor):
        def __init__(self, name: str, parent: YoloOrientedboxExtractor):
            super().__init__()
            self._name = name
            self._parent = parent
            self.items: Dict[str, Union[str, DatasetItem]] = OrderedDict()

        def __iter__(self):
            for item_id in self.items:
                item = self._parent._get(item_id, self._name)
                if item is not None:
                    yield item

        def __len__(self):
            return len(self.items)

        def categories(self):
            return self._parent.categories()

    def __init__(
        self,
        config_path: str,
        image_info: Union[None, str, ImageMeta] = None,
        urls: Optional[List[str]] = None,
        *,
        subset: Optional[str] = None
    ) -> None:
        META_FILE = YoloOrientedboxPath.META_FILE

        if not osp.isdir(config_path):
            raise DatasetImportError(f"{config_path} should be a directory.")
        
        if not urls:
            raise DatasetImportError(
                f"`urls` should be specified for {self.__class__.__name__}, "
                f"if you want to import a dataset with using this {self.__class__.__name__} directly. "
                "In most case, it happens by giving an incorrect format name to the import interface. "
                "Please consider to import your dataset with this format name, 'yolo', "
                "such as `Dataset.import_from(..., format='yolo')`."
            )

        super().__init__(subset=subset)

        rootpath = self._get_rootpath(config_path)
        self._path = rootpath

        self._image_info = self.parse_image_info(rootpath, image_info)
        self._urls = urls
        self._img_files = self._load_img_files(rootpath)
        self._ann_types = set()
        
        self._categories = {
            AnnotationType.label: self._load_categories(
                osp.join(self._path, META_FILE)
            )
        }

    def __iter__(self) -> Iterator[DatasetItem]:
        label_categories = self._categories.get(AnnotationType.label)
        if label_categories is None:
            raise DatasetImportError("label_categories should be not None.")

        pbar = self._ctx.progress_reporter
        for url in pbar.iter(self._urls, desc=f"Importing '{self._subset}'"):
            try:
                fname = self._get_fname(url)
                img = Image(path=self._img_files[fname])
                anns = self._parse_annotations(
                    url,
                    img,
                    item_id=(fname, self._subset)
                )
                yield DatasetItem(id=fname, subset=self._subset, media=img, annotations=anns)

                for ann in anns:
                    self._ann_types.add(ann.type)
            except Exception as e:
                self._ctx.error_policy.report_item_error(e, item_id=(fname, self._subset))

    @staticmethod
    def localize_path(path: str) -> str:
        """
        Removes the "data/" prefix from the path
        """

        path = osp.normpath(path.strip()).replace("\\", "/")
        default_base = "data/"
        if path.startswith(default_base):
            path = path[len(default_base) :]
        return path

    @classmethod
    def name_from_path(cls, path: str) -> str:
        """
        Obtains <image name> from the path like [data/]<subset>_obj/<image_name>.ext

        <image name> can be <a/b/c/filename>, so it is
        more involved than just calling "basename()".
        """

        path = cls.localize_path(path)

        parts = split_path(path)
        if 1 < len(parts) and not osp.isabs(path):
            path = osp.join(*parts[1:])  # pylint: disable=no-value-for-parameter

        return osp.splitext(path)[0]

    @classmethod
    def _image_loader(cls, *args, **kwargs):
        return load_image(*args, **kwargs, keep_exif=True)

    def _get(self, item_id: str, subset_name: str) -> Optional[DatasetItem]:
        subset = self._subsets[subset_name]
        item = subset.items[item_id]

        if isinstance(item, str):
            try:
                image_size = self._image_info.get(item_id)
                image_path = osp.join(self._path, item)

                if image_size:
                    image = Image(path=image_path, size=image_size)
                else:
                    image = Image(path=image_path, data=self._image_loader)

                anno_path = osp.splitext(image.path)[0] + ".txt"
                annotations, angle = self._parse_annotations(
                    anno_path, image, item_id=(item_id, subset_name)
                )

                item = DatasetItem(
                    id=item_id, subset=subset_name, media=image, annotations=annotations
                )
                subset.items[item_id] = item
            except (UndeclaredLabelError, InvalidAnnotationError) as e:
                self._ctx.error_policy.report_annotation_error(e, item_id=(item_id, self._subset_name))
            except Exception as e:
                self._ctx.error_policy.report_item_error(e, item_id=(item_id, subset_name))
                subset.items.pop(item_id)
                item = None

        return item

    # TODO
    def __len__(self):
        return sum(len(s) for s in self._subsets.values())
    
    def _get_fname(self, fpath: str) -> str:
        return osp.splitext(osp.basename(fpath))[0]

    def _load_img_files(self, rootpath: str) -> Dict[str, str]:
        return {
            self._get_fname(img_file): img_file
            for img_file in find_files(rootpath, IMAGE_EXTENSIONS, recursive=True, max_depth=2)
            if extract_subset_name_from_parent(img_file, rootpath) == self._subset
        }

    def get_subset(self, name):
        return self._subsets[name]

    @staticmethod
    def parse_image_info(
        rootpath: str, image_info: Optional[Union[str, ImageMeta]] = None
    ) -> ImageMeta:
        assert image_info is None or isinstance(image_info, (str, dict))
        if image_info is None:
            image_info = osp.join(rootpath, DEFAULT_IMAGE_META_FILE_NAME)
            if not osp.isfile(image_info):
                image_info = {}
        if isinstance(image_info, str):
            image_info = load_image_meta_file(image_info)

        return image_info

    @staticmethod
    def _parse_field(value: str, cls: Type[T], field_name: str) -> T:
        try:
            return cls(value)
        except Exception as e:
            raise InvalidAnnotationError(
                f"Can't parse {field_name} from '{value}'. Expected {cls}"
            ) from e

    def _parse_annotations(
        self, anno_path: str, image: Image, *, item_id: Tuple[str, str]
    ) -> List[Annotation]:
        lines = []
        with open(anno_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    lines.append(line)

        annotations = []

        if lines:
            # Use image info as late as possible to avoid unnecessary image loading
            if image.size is None:
                raise DatasetImportError(f"Can't find image info for '{self.localize_path(image.path)}'"
                )
            image_height, image_width = image.size

            for idx, line in enumerate(lines):
                try:
                    parts = line.split()
                    if len(parts) != 9:
                        raise InvalidAnnotationError(
                            f"Unexpected field count {len(parts)} in the oriented bbox description. "
                            "Expected 9 fields (label, x1, y1, x2, y2, x3, y3, x4, y4)."
                        )
                    label_id, x1, y1, x2, y2, x3, y3, x4, y4 = parts

                    label_id = self._parse_field(label_id, int, "oriented bbox label id")
                    if label_id not in self._categories[AnnotationType.label]:
                        raise UndeclaredLabelError(str(label_id))
                    
                    x1 = self._parse_field(x1, float, "oriented bbox x1")
                    y1 = self._parse_field(y1, float, "oriented bbox y1")
                    x2 = self._parse_field(x2, float, "oriented bbox x2")
                    y2 = self._parse_field(y2, float, "oriented bbox y2")
                    x3 = self._parse_field(x3, float, "oriented bbox x3")
                    y3 = self._parse_field(y3, float, "oriented bbox y3")
                    x4 = self._parse_field(x4, float, "oriented bbox x4")
                    y4 = self._parse_field(y4, float, "oriented bbox y4")

                    (x, y), (w, h), r = xyxyxyxy2xywhr(np.array([[[x1 * image_width, y1 * image_height], [x2 * image_width, y2 * image_height], [x3 * image_width, y3 * image_height], [x4 * image_width, y4 * image_height]]]))

                    annotations.append(
                        Bbox(
                            x,
                            y,
                            w,
                            h,
                            label=label_id,
                            id=idx,
                            group=idx,
                            attributes={"angle": r},
                        )
                    )
                except Exception as e:
                    self._ctx.error_policy.report_annotation_error(e, item_id=item_id)

        return annotations

    def _get_rootpath(self, config_path: str) -> str:
        return config_path


    def _load_categories(self, names_path: str) -> LabelCategories:
        if has_meta_file(osp.dirname(names_path)):
            return LabelCategories.from_iterable(parse_meta_file(osp.dirname(names_path)).keys())

        label_categories = LabelCategories()

        with open(names_path, "r") as fp:
            loaded = yaml.safe_load(fp.read())
            if isinstance(loaded["names"], list):
                label_names = loaded["names"]
            elif isinstance(loaded["names"], dict):
                label_names = list(loaded["names"].values())
            else:
                raise DatasetImportError(f"Can't read dataset category file '{names_path}'")

        for label_name in label_names:
            label_categories.add(label_name)

        return label_categories