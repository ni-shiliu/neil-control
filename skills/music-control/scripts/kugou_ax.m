#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>

static NSString *StringValue(CFTypeRef value) {
    if (!value) return @"";
    if (CFGetTypeID(value) == CFStringGetTypeID()) {
        return [(__bridge NSString *)value copy];
    }
    if (CFGetTypeID(value) == CFNumberGetTypeID()) {
        return [(__bridge NSNumber *)value stringValue];
    }
    return @"";
}

static NSString *Attribute(AXUIElementRef element, CFStringRef name) {
    CFTypeRef value = NULL;
    AXError error = AXUIElementCopyAttributeValue(element, name, &value);
    if (error != kAXErrorSuccess || !value) return @"";
    NSString *result = StringValue(value);
    CFRelease(value);
    return result;
}

static NSArray *Children(AXUIElementRef element) {
    CFTypeRef value = NULL;
    AXError error = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute, &value);
    if (error != kAXErrorSuccess || !value) return @[];
    NSArray *result = [(__bridge NSArray *)value copy];
    CFRelease(value);
    return result;
}

static NSArray *Actions(AXUIElementRef element) {
    CFArrayRef names = NULL;
    AXError error = AXUIElementCopyActionNames(element, &names);
    if (error != kAXErrorSuccess || !names) return @[];
    NSArray *result = [(__bridge NSArray *)names copy];
    CFRelease(names);
    return result;
}

static BOOL HasAction(AXUIElementRef element, NSString *action) {
    return [Actions(element) containsObject:action];
}

static BOOL Perform(AXUIElementRef element, CFStringRef action) {
    return AXUIElementPerformAction(element, action) == kAXErrorSuccess;
}

static BOOL CenterPoint(AXUIElementRef element, CGPoint *point) {
    CFTypeRef positionValue = NULL;
    CFTypeRef sizeValue = NULL;
    AXError positionError = AXUIElementCopyAttributeValue(
        element, kAXPositionAttribute, &positionValue);
    AXError sizeError = AXUIElementCopyAttributeValue(
        element, kAXSizeAttribute, &sizeValue);
    CGPoint position = CGPointZero;
    CGSize size = CGSizeZero;
    BOOL ok = positionError == kAXErrorSuccess &&
              sizeError == kAXErrorSuccess &&
              positionValue && sizeValue &&
              AXValueGetValue(positionValue, kAXValueCGPointType, &position) &&
              AXValueGetValue(sizeValue, kAXValueCGSizeType, &size);
    if (positionValue) CFRelease(positionValue);
    if (sizeValue) CFRelease(sizeValue);
    if (ok) *point = CGPointMake(position.x + size.width / 2.0,
                                 position.y + size.height / 2.0);
    return ok;
}

static BOOL ClickForPid(AXUIElementRef element, pid_t pid) {
    CGPoint point;
    if (!CenterPoint(element, &point)) return NO;
    CGEventRef move = CGEventCreateMouseEvent(
        NULL, kCGEventMouseMoved, point, kCGMouseButtonLeft);
    CGEventRef down = CGEventCreateMouseEvent(
        NULL, kCGEventLeftMouseDown, point, kCGMouseButtonLeft);
    CGEventRef up = CGEventCreateMouseEvent(
        NULL, kCGEventLeftMouseUp, point, kCGMouseButtonLeft);
    if (!move || !down || !up) {
        if (move) CFRelease(move);
        if (down) CFRelease(down);
        if (up) CFRelease(up);
        return NO;
    }
    CGEventPostToPid(pid, move);
    CGEventPostToPid(pid, down);
    CGEventPostToPid(pid, up);
    CFRelease(move);
    CFRelease(down);
    CFRelease(up);
    return YES;
}

static void Walk(AXUIElementRef element, void (^visitor)(AXUIElementRef, BOOL *), BOOL *stop) {
    if (*stop) return;
    visitor(element, stop);
    if (*stop) return;
    for (id child in Children(element)) {
        Walk((__bridge AXUIElementRef)child, visitor, stop);
        if (*stop) return;
    }
}

static AXUIElementRef Find(AXUIElementRef root, BOOL (^predicate)(AXUIElementRef)) {
    __block AXUIElementRef found = NULL;
    BOOL stop = NO;
    Walk(root, ^(AXUIElementRef element, BOOL *shouldStop) {
        if (predicate(element)) {
            found = (AXUIElementRef)CFRetain(element);
            *shouldStop = YES;
        }
    }, &stop);
    return found;
}

static NSArray *FindAll(AXUIElementRef root, BOOL (^predicate)(AXUIElementRef)) {
    NSMutableArray *found = [NSMutableArray array];
    BOOL stop = NO;
    Walk(root, ^(AXUIElementRef element, BOOL *shouldStop) {
        (void)shouldStop;
        if (predicate(element)) [found addObject:(__bridge id)element];
    }, &stop);
    return found;
}

static AXUIElementRef KuGouApplication(pid_t *outPid) {
    NSArray *apps = [NSRunningApplication
        runningApplicationsWithBundleIdentifier:@"com.kugou.mac.Music"];
    if (apps.count == 0) return NULL;
    pid_t pid = ((NSRunningApplication *)apps.firstObject).processIdentifier;
    if (outPid) *outPid = pid;
    return AXUIElementCreateApplication(pid);
}

static void PrintJSON(NSDictionary *payload) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
}

static NSDictionary *ElementInfo(AXUIElementRef element) {
    return @{
        @"role": Attribute(element, kAXRoleAttribute),
        @"title": Attribute(element, kAXTitleAttribute),
        @"description": Attribute(element, kAXDescriptionAttribute),
        @"value": Attribute(element, kAXValueAttribute),
        @"help": Attribute(element, kAXHelpAttribute),
        @"actions": Actions(element),
    };
}

static int Dump(AXUIElementRef app) {
    NSMutableArray *items = [NSMutableArray array];
    BOOL stop = NO;
    Walk(app, ^(AXUIElementRef element, BOOL *shouldStop) {
        (void)shouldStop;
        NSDictionary *info = ElementInfo(element);
        if ([info[@"role"] length] || [info[@"description"] length]) {
            [items addObject:info];
        }
    }, &stop);
    PrintJSON(@{@"ok": @YES, @"elements": items});
    return 0;
}

static AXUIElementRef SearchField(AXUIElementRef app) {
    return Find(app, ^BOOL(AXUIElementRef element) {
        return [Attribute(element, kAXRoleAttribute) isEqualToString:(__bridge NSString *)kAXTextFieldRole];
    });
}

static void PostKeyForPid(pid_t pid, CGKeyCode keyCode, CGEventFlags flags) {
    CGEventRef down = CGEventCreateKeyboardEvent(NULL, keyCode, true);
    CGEventRef up = CGEventCreateKeyboardEvent(NULL, keyCode, false);
    if (!down || !up) {
        if (down) CFRelease(down);
        if (up) CFRelease(up);
        return;
    }
    CGEventSetFlags(down, flags);
    CGEventSetFlags(up, flags);
    CGEventPostToPid(pid, down);
    CGEventPostToPid(pid, up);
    CFRelease(down);
    CFRelease(up);
}

static void PostTextForPid(pid_t pid, NSString *text) {
    NSUInteger length = text.length;
    if (!length) return;
    UniChar *characters = calloc(length, sizeof(UniChar));
    [text getCharacters:characters range:NSMakeRange(0, length)];
    CGEventRef down = CGEventCreateKeyboardEvent(NULL, 0, true);
    CGEventRef up = CGEventCreateKeyboardEvent(NULL, 0, false);
    if (down && up) {
        CGEventKeyboardSetUnicodeString(down, length, characters);
        CGEventKeyboardSetUnicodeString(up, length, characters);
        CGEventPostToPid(pid, down);
        CGEventPostToPid(pid, up);
    }
    if (down) CFRelease(down);
    if (up) CFRelease(up);
    free(characters);
}

static BOOL SubmitInBackground(AXUIElementRef app, AXUIElementRef field,
                               pid_t pid, NSString *query) {
    ClickForPid(field, pid);
    AXUIElementSetAttributeValue(field, kAXFocusedAttribute, kCFBooleanTrue);
    AXUIElementSetAttributeValue(app, kAXFocusedUIElementAttribute, field);
    PostKeyForPid(pid, 0, kCGEventFlagMaskCommand);  // Command-A
    PostTextForPid(pid, query);
    usleep(350000);                                 // Wait for autocomplete.
    AXUIElementRef suggestion = Find(app, ^BOOL(AXUIElementRef element) {
        NSString *value = [Attribute(element, kAXValueAttribute)
            stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        return [value isEqualToString:query] &&
               HasAction(element, (__bridge NSString *)kAXPressAction);
    });
    BOOL submitted = suggestion &&
        (ClickForPid(suggestion, pid) || Perform(suggestion, kAXPressAction));
    if (suggestion) CFRelease(suggestion);
    if (!submitted) {
        PostKeyForPid(pid, 36, 0);                  // Last-resort Return.
    }
    usleep(180000);
    return submitted;
}

static int Search(AXUIElementRef app, pid_t pid, NSString *query) {
    AXUIElementRef field = SearchField(app);
    if (!field) {
        PrintJSON(@{@"ok": @NO, @"error": @"search_field_not_found"});
        return 1;
    }
    AXError setError = AXUIElementSetAttributeValue(
        field, kAXValueAttribute, (__bridge CFTypeRef)query);
    AXUIElementSetAttributeValue(field, kAXFocusedAttribute, kCFBooleanTrue);
    AXUIElementSetAttributeValue(app, kAXFocusedUIElementAttribute, field);
    BOOL submitted = Perform(field, kAXConfirmAction);
    if (!submitted) submitted = SubmitInBackground(app, field, pid, query);
    CFRelease(field);
    if (setError != kAXErrorSuccess) {
        PrintJSON(@{@"ok": @NO, @"error": @"search_field_not_set",
                    @"ax_error": @(setError)});
        return 1;
    }
    PrintJSON(@{@"ok": @YES, @"submitted": @(submitted), @"query": query});
    return submitted ? 0 : 2;
}

static BOOL LooksLikeSongRow(AXUIElementRef element) {
    NSString *role = Attribute(element, kAXRoleAttribute);
    NSString *value = Attribute(element, kAXValueAttribute);
    if (![role isEqualToString:(__bridge NSString *)kAXGroupRole]) return NO;
    NSRegularExpression *duration = [NSRegularExpression
        regularExpressionWithPattern:@"\\b\\d{2}:\\d{2}\\b" options:0 error:nil];
    return [duration firstMatchInString:value options:0
                                  range:NSMakeRange(0, value.length)] != nil;
}

static NSArray *LeafTexts(AXUIElementRef root) {
    NSMutableArray *texts = [NSMutableArray array];
    BOOL stop = NO;
    Walk(root, ^(AXUIElementRef element, BOOL *shouldStop) {
        (void)shouldStop;
        if (![Attribute(element, kAXRoleAttribute)
                isEqualToString:(__bridge NSString *)kAXStaticTextRole]) return;
        NSString *value = [Attribute(element, kAXValueAttribute)
            stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (value.length) [texts addObject:value];
    }, &stop);
    return texts;
}

static NSString *CurrentSearchQuery(AXUIElementRef app) {
    AXUIElementRef header = Find(app, ^BOOL(AXUIElementRef element) {
        NSString *value = Attribute(element, kAXValueAttribute);
        return [value containsString:@"搜索“"] && [value containsString:@"”的相关歌曲"];
    });
    if (!header) return @"";
    NSString *value = Attribute(header, kAXValueAttribute);
    CFRelease(header);
    NSRange start = [value rangeOfString:@"“"];
    NSRange end = [value rangeOfString:@"”的相关歌曲"];
    if (start.location == NSNotFound || end.location == NSNotFound ||
        end.location <= NSMaxRange(start)) return @"";
    NSString *query = [value substringWithRange:NSMakeRange(
        NSMaxRange(start), end.location - NSMaxRange(start))];
    return [query stringByTrimmingCharactersInSet:
        NSCharacterSet.whitespaceAndNewlineCharacterSet];
}

static int Results(AXUIElementRef app) {
    NSArray *rows = FindAll(app, ^BOOL(AXUIElementRef element) {
        return LooksLikeSongRow(element);
    });
    NSMutableArray *results = [NSMutableArray array];
    NSInteger index = 0;
    for (id item in rows) {
        AXUIElementRef row = (__bridge AXUIElementRef)item;
        NSString *value = Attribute(row, kAXValueAttribute);
        if (!value.length) continue;
        [results addObject:@{
            @"index": @(index++),
            @"text": value,
            @"parts": LeafTexts(row),
            @"actions": Actions(row),
        }];
    }
    PrintJSON(@{@"ok": @YES, @"query": CurrentSearchQuery(app),
                @"results": results});
    return 0;
}

static AXUIElementRef FindControl(AXUIElementRef app, NSArray<NSString *> *descriptions) {
    return Find(app, ^BOOL(AXUIElementRef element) {
        NSString *description = Attribute(element, kAXDescriptionAttribute);
        return [descriptions containsObject:description] &&
               HasAction(element, (__bridge NSString *)kAXPressAction);
    });
}

static int Control(AXUIElementRef app, NSString *command) {
    NSDictionary *names = @{
        @"next": @[@"下一首"],
        @"prev": @[@"上一首"],
        @"toggle": @[@"播放", @"暂停"],
        @"play": @[@"播放"],
        @"pause": @[@"暂停"],
    };
    NSArray *descriptions = names[command];
    if (!descriptions) {
        PrintJSON(@{@"ok": @NO, @"error": @"unknown_control"});
        return 1;
    }
    AXUIElementRef control = FindControl(app, descriptions);
    if (!control) {
        PrintJSON(@{@"ok": @NO, @"error": @"control_not_found"});
        return 1;
    }
    BOOL ok = Perform(control, kAXPressAction);
    CFRelease(control);
    PrintJSON(@{@"ok": @(ok), @"action": command});
    return ok ? 0 : 2;
}

static int Status(AXUIElementRef app) {
    AXUIElementRef toggle = FindControl(app, @[@"播放", @"暂停"]);
    AXUIElementRef title = Find(app, ^BOOL(AXUIElementRef element) {
        NSString *role = Attribute(element, kAXRoleAttribute);
        NSString *value = Attribute(element, kAXValueAttribute);
        return [role isEqualToString:(__bridge NSString *)kAXStaticTextRole] &&
               [value containsString:@" - "] &&
               ![value containsString:@" / "];
    });
    if (!title) {
        if (toggle) CFRelease(toggle);
        PrintJSON(@{@"ok": @NO, @"error": @"no_player"});
        return 1;
    }
    NSString *track = Attribute(title, kAXValueAttribute);
    NSArray *parts = [track componentsSeparatedByString:@" - "];
    NSString *artist = parts.count > 1 ? parts.firstObject : @"";
    NSString *song = parts.count > 1
        ? [[parts subarrayWithRange:NSMakeRange(1, parts.count - 1)]
              componentsJoinedByString:@" - "]
        : track;
    NSString *toggleDescription = toggle
        ? Attribute(toggle, kAXDescriptionAttribute) : @"";
    // KuGou exposes the current state here (not the action to perform).
    BOOL playing = [toggleDescription isEqualToString:@"播放"];
    PrintJSON(@{@"ok": @YES, @"title": song, @"artist": artist,
                @"album": @"", @"playing": @(playing),
                @"source": @"kugou_accessibility"});
    CFRelease(title);
    if (toggle) CFRelease(toggle);
    return 0;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            PrintJSON(@{@"ok": @NO, @"error": @"missing_command"});
            return 64;
        }
        NSDictionary *options = @{
            (__bridge NSString *)kAXTrustedCheckOptionPrompt: @NO
        };
        if (!AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options)) {
            PrintJSON(@{@"ok": @NO, @"error": @"accessibility_permission",
                        @"message": @"请在系统设置 > 隐私与安全性 > 辅助功能中授权当前终端或 Codex"});
            return 77;
        }
        pid_t pid = 0;
        AXUIElementRef app = KuGouApplication(&pid);
        if (!app) {
            PrintJSON(@{@"ok": @NO, @"error": @"kugou_not_running"});
            return 69;
        }
        NSString *command = [NSString stringWithUTF8String:argv[1]];
        int result = 0;
        if ([command isEqualToString:@"dump"]) {
            result = Dump(app);
        } else if ([command isEqualToString:@"search"] && argc >= 3) {
            result = Search(app, pid, [NSString stringWithUTF8String:argv[2]]);
        } else if ([command isEqualToString:@"results"]) {
            result = Results(app);
        } else if ([command isEqualToString:@"control"] && argc >= 3) {
            result = Control(app, [NSString stringWithUTF8String:argv[2]]);
        } else if ([command isEqualToString:@"status"]) {
            result = Status(app);
        } else {
            PrintJSON(@{@"ok": @NO, @"error": @"invalid_arguments"});
            result = 64;
        }
        CFRelease(app);
        return result;
    }
}
